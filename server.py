"""
Hive Mind — FastAPI gateway server.

Thin HTTP/WebSocket layer over the session manager.
All Claude CLI interaction flows through here.
"""

import asyncio
import json
import logging
import os
import secrets as _secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from config import config
from core.hitl import hitl_store, DEFAULT_TTL
from core.models import ModelRegistry, Provider
from core.sessions import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("hive-mind.server")

# ---------------------------------------------------------------------------
# Keyring → env bridge: expose keyring secrets as env vars so non-Python
# consumers (e.g. Claude Code reading .mcp.container.json) can resolve them.
# ---------------------------------------------------------------------------
_KEYRING_ENV_KEYS = ["MCP_AUTH_TOKEN", "HITL_INTERNAL_TOKEN", "GITHUB_TOKEN"]

try:
    import keyring as _kr
    for _k in _KEYRING_ENV_KEYS:
        if _k not in os.environ:
            _v = _kr.get_password("hive-mind", _k)
            if _v:
                os.environ[_k] = _v
except Exception:
    pass  # keyring unavailable — fall through to env_file / .env

# ---------------------------------------------------------------------------
# Bootstrap model registry from config
# ---------------------------------------------------------------------------
def _build_registry() -> ModelRegistry:
    providers = {}
    for name, pconf in config.providers.items():
        if isinstance(pconf, dict):
            providers[name] = Provider(
                name=name,
                env_overrides=pconf.get("env", {}),
                api_base=pconf.get("api_base"),
            )
        else:
            providers[name] = Provider(name=name)
    return ModelRegistry(providers=providers, static_models=config.models)


model_registry = _build_registry()
session_mgr = SessionManager(model_registry)


_hitl_cleanup_task: asyncio.Task | None = None
# Track Telegram messages for HITL tokens: token -> (chat_id, message_id, original_text)
_hitl_messages: dict[str, tuple[int, int, str]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _hitl_cleanup_task
    await session_mgr.start()
    _hitl_cleanup_task = asyncio.create_task(_hitl_cleanup_loop())
    log.info("Gateway started on port %d", config.server_port)
    yield
    _hitl_cleanup_task.cancel()
    await session_mgr.shutdown()


async def _hitl_cleanup_loop():
    """Periodically purge expired HITL tokens and update Telegram messages."""
    while True:
        await asyncio.sleep(30)
        expired_tokens = hitl_store.cleanup_expired()
        for token in expired_tokens:
            await _edit_hitl_message(token, "Expired")


app = FastAPI(title="Hive Mind Gateway", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class CreateSessionRequest(BaseModel):
    owner_type: str
    owner_ref: str
    client_ref: str
    model: str | None = None
    surface_prompt: str | None = None
    allowed_directories: list[str] | None = None
    mind_id: str = "ada"


class ImageAttachment(BaseModel):
    data: str        # base64-encoded image bytes
    media_type: str  # e.g. "image/jpeg", "image/png"


class MessageRequest(BaseModel):
    content: str
    images: list[ImageAttachment] = []


class ModelSwitchRequest(BaseModel):
    model: str


class ActivateRequest(BaseModel):
    client_type: str
    client_ref: str


class RemoteControlResponse(BaseModel):
    url: str
    session_id: str
    rc_pid: int


class CreateGroupSessionRequest(BaseModel):
    moderator_mind_id: str = "ada"
    surface_prompt: str | None = None


class GroupSessionMessageRequest(BaseModel):
    content: str
    images: list[dict] | None = None


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------
@app.post("/sessions")
async def create_session(body: CreateSessionRequest):
    session = await session_mgr.create_session(
        owner_type=body.owner_type,
        owner_ref=body.owner_ref,
        client_ref=body.client_ref,
        model=body.model,
        surface_prompt=body.surface_prompt,
        allowed_directories=body.allowed_directories,
        mind_id=body.mind_id,
    )
    return session


@app.get("/sessions")
async def list_sessions(
    owner_ref: str | None = None,
    status: str | None = None,
    client_type: str | None = None,
    client_ref: str | None = None,
):
    return await session_mgr.list_sessions(
        owner_ref=owner_ref,
        status=status,
        client_type=client_type,
        client_ref=client_ref,
    )


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    session = await session_mgr.get_session(session_id)
    if not session:
        return {"error": "Session not found"}, 404
    return session


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    return await session_mgr.kill_session(session_id)


# ---------------------------------------------------------------------------
# Message endpoint (SSE streaming)
# ---------------------------------------------------------------------------
@app.post("/sessions/{session_id}/message")
async def send_message(session_id: str, body: MessageRequest):
    images = [{"media_type": img.media_type, "data": img.data} for img in body.images] if body.images else None
    log.info("message: session=%s chars=%d", session_id, len(body.content))
    t0 = time.monotonic()

    async def event_stream():
        async for event in session_mgr.send_message(session_id, body.content, images=images):
            yield f"data: {json.dumps(event)}\n\n"
        log.info("message: done session=%s elapsed=%.1fs", session_id, time.monotonic() - t0)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Session management endpoints
# ---------------------------------------------------------------------------
@app.post("/sessions/{session_id}/activate")
async def activate_session(session_id: str, body: ActivateRequest):
    return await session_mgr.activate_session(
        session_id, body.client_type, body.client_ref
    )


@app.post("/sessions/{session_id}/model")
async def switch_model(session_id: str, body: ModelSwitchRequest):
    return await session_mgr.switch_model(session_id, body.model)


@app.post("/sessions/{session_id}/autopilot")
async def toggle_autopilot(session_id: str):
    return await session_mgr.toggle_autopilot(session_id)


# ---------------------------------------------------------------------------
# Remote Control endpoints
# ---------------------------------------------------------------------------
@app.post("/sessions/{session_id}/remote-control", response_model=RemoteControlResponse)
async def start_remote_control(session_id: str):
    """Spawn a Remote Control subprocess and return the session URL."""
    try:
        result = await session_mgr.spawn_rc_process(session_id)
        return result
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except (TimeoutError, RuntimeError) as e:
        return JSONResponse({"error": str(e)}, status_code=504)


@app.delete("/sessions/{session_id}/remote-control")
async def stop_remote_control(session_id: str):
    """Stop the Remote Control subprocess for a session."""
    await session_mgr.kill_rc_process(session_id)
    return {"ok": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# Group session endpoints
# ---------------------------------------------------------------------------

# In-memory surface_prompt store (keyed by group_session_id).
# Avoids a DB migration — surface_prompt is a thin channel-context hint,
# not data that needs to survive server restarts.
_group_surface_prompts: dict[str, str] = {}


@app.post("/group-sessions")
async def create_group_session(body: CreateGroupSessionRequest):
    result = await session_mgr.create_group_session(body.moderator_mind_id)
    if body.surface_prompt:
        _group_surface_prompts[result["id"]] = body.surface_prompt
    return result


@app.get("/group-sessions/{group_session_id}")
async def get_group_session(group_session_id: str):
    result = await session_mgr.get_group_session(group_session_id)
    if not result:
        return JSONResponse({"error": "Group session not found"}, status_code=404)
    # Include transcript
    transcript = await session_mgr.get_group_transcript(group_session_id)
    result["transcript"] = transcript
    return result


@app.post("/group-sessions/{group_session_id}/message")
async def send_group_message(group_session_id: str, body: GroupSessionMessageRequest):
    """Send a message to the moderator's session in the group."""
    group = await session_mgr.get_group_session(group_session_id)
    if not group:
        return JSONResponse({"error": "Group session not found"}, status_code=404)

    moderator_mind_id = group["moderator_mind_id"]

    # Combine stored surface_prompt (channel context) with moderator role hint
    base_prompt = f"You are the moderator for group session {group_session_id}."
    stored = _group_surface_prompts.get(group_session_id)
    moderator_prompt = f"{stored}\n\n{base_prompt}" if stored else base_prompt

    # Find or create the moderator's child session
    child_session_id = await session_mgr.get_or_create_group_child_session(
        group_session_id, moderator_mind_id, surface_prompt=moderator_prompt
    )

    # Prepend /moderate so the Claude Code harness invokes the skill automatically
    routed_content = f"/moderate {body.content}"

    # Pass images through if provided
    images = body.images or None

    async def event_stream():
        async for event in session_mgr.send_message(child_session_id, routed_content, images=images):
            event.setdefault("mind_id", moderator_mind_id)
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.delete("/group-sessions/{group_session_id}")
async def delete_group_session(group_session_id: str):
    try:
        return await session_mgr.delete_group_session(group_session_id)
    except ValueError:
        return JSONResponse({"error": "Group session not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Model listing
# ---------------------------------------------------------------------------
@app.get("/models")
async def list_models():
    return await model_registry.list_models()


# ---------------------------------------------------------------------------
# Memory Expiry
# ---------------------------------------------------------------------------
@app.post("/memory/expiry-sweep")
async def memory_expiry_sweep(x_hitl_internal: str = Header(None)):
    """Trigger memory expiry sweep for expired timed-event entries."""
    if not config.hitl_internal_token:
        return JSONResponse({"error": "HITL not configured"}, status_code=500)
    if x_hitl_internal != config.hitl_internal_token:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from core.memory_expiry import sweep_expired_events
    results = await asyncio.to_thread(sweep_expired_events)
    return results


# ---------------------------------------------------------------------------
# Epilogue Sweep
# ---------------------------------------------------------------------------
@app.post("/epilogue/sweep")
async def epilogue_sweep(x_hitl_internal: str = Header(None)):
    """Trigger epilogue processing for all pending sessions."""
    if not config.hitl_internal_token:
        return JSONResponse({"error": "HITL not configured"}, status_code=500)
    if x_hitl_internal != config.hitl_internal_token:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from core.epilogue import process_pending_sessions
    results = await process_pending_sessions(session_mgr, config.epilogue_thresholds)
    return results


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/sessions/{session_id}/stream")
async def ws_stream(ws: WebSocket, session_id: str):
    await ws.accept()
    try:
        while True:
            data = await ws.receive_json()
            images = data.get("images")
            async for event in session_mgr.send_message(session_id, data["content"], images=images):
                await ws.send_json(event)
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# Slash command routing (used by clients)
# ---------------------------------------------------------------------------
SERVER_COMMANDS = {"/clear", "/model", "/autopilot", "/kill", "/status", "/sessions", "/switch", "/new", "/remember"}


class CommandRequest(BaseModel):
    content: str
    owner_type: str = "terminal"
    owner_ref: str = ""
    client_ref: str = ""


@app.post("/command")
async def route_command(body: CommandRequest):
    """Route slash commands — server-handled or passthrough to CLI."""
    content = body.content.strip()
    parts = content.split()
    cmd = parts[0] if parts and parts[0].startswith("/") else None

    if cmd not in SERVER_COMMANDS:
        return {"error": "Not a server command. Send as a regular message."}

    try:
        return await _handle_command(cmd, parts, body)
    except ValueError as e:
        return {"error": str(e)}
    except Exception:
        log.exception("Error handling command: %s", content)
        return {"error": "Internal server error"}


async def _handle_command(cmd: str, parts: list[str], body: CommandRequest):

    if cmd == "/status":
        sessions = await session_mgr.list_sessions()
        running = sum(1 for s in sessions if s["status"] == "running")
        return {
            "server_port": config.server_port,
            "default_model": config.default_model,
            "total_sessions": len(sessions),
            "running_sessions": running,
        }

    if cmd == "/sessions":
        return await session_mgr.list_sessions(owner_ref=body.owner_ref)

    if cmd in ("/new", "/clear"):
        # Kill active session (if any), run memory pipeline on it, then create a new one.
        # _run_memory_for_owner blocks inside create_session until the pipeline finishes.
        active = await session_mgr.get_active_session(body.owner_type, body.client_ref)
        if active:
            await session_mgr.kill_session(active["id"])
        allowed_directories = parts[1:] if len(parts) > 1 else None
        return await session_mgr.create_session(
            owner_type=body.owner_type,
            owner_ref=body.owner_ref,
            client_ref=body.client_ref,
            allowed_directories=allowed_directories,
        )

    if cmd == "/model":
        if len(parts) < 2:
            return await model_registry.list_models()
        model_name = parts[1]
        active = await session_mgr.get_active_session(body.owner_type, body.client_ref)
        if not active:
            return {"error": "No active session. Use /new first."}
        return await session_mgr.switch_model(active["id"], model_name)

    if cmd == "/autopilot":
        active = await session_mgr.get_active_session(body.owner_type, body.client_ref)
        if not active:
            return {"error": "No active session. Use /new first."}
        return await session_mgr.toggle_autopilot(active["id"])

    if cmd == "/switch":
        if len(parts) < 2:
            return {"error": "Usage: /switch <session_id or number>"}
        target = parts[1]
        # If numeric, resolve from user's session list
        if target.isdigit():
            sessions = await session_mgr.list_sessions(owner_ref=body.owner_ref)
            idx = int(target) - 1
            if 0 <= idx < len(sessions):
                target = sessions[idx]["id"]
            else:
                return {"error": f"Invalid session number: {target}"}
        return await session_mgr.activate_session(
            target, body.owner_type, body.client_ref
        )

    if cmd == "/kill":
        if len(parts) < 2:
            return {"error": "Usage: /kill <session_id or number>"}
        target = parts[1]
        if target.isdigit():
            sessions = await session_mgr.list_sessions(owner_ref=body.owner_ref)
            idx = int(target) - 1
            if 0 <= idx < len(sessions):
                target = sessions[idx]["id"]
            else:
                return {"error": f"Invalid session number: {target}"}
        return await session_mgr.kill_session(target)

    if cmd == "/remember":
        return {
            "response": (
                "The memory pipeline runs automatically when you start a new session with /new. "
                "To save something specific right now, say 'remember this' in the conversation."
            )
        }

    return {"error": f"Unknown command: {cmd}"}


# ---------------------------------------------------------------------------
# LinkedIn OAuth
# ---------------------------------------------------------------------------
_LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
_LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
_LINKEDIN_REDIRECT_URI = "https://sparktobloom.com/linkedin/callback"
_LINKEDIN_SCOPES = "openid profile email w_member_social"
_LINKEDIN_TOKEN_PATH = Path("/home/USER/Storage/Dev/hive_mind_mcp/credentials/linkedin_token.json")

_linkedin_oauth_states: set[str] = set()


def _get_linkedin_creds() -> tuple[str | None, str | None]:
    try:
        import keyring as _kr
        cid = _kr.get_password("hive-mind", "LINKEDIN_CLIENT_ID")
        csec = _kr.get_password("hive-mind", "LINKEDIN_CLIENT_SECRET")
        return cid, csec
    except Exception:
        return os.getenv("LINKEDIN_CLIENT_ID"), os.getenv("LINKEDIN_CLIENT_SECRET")


@app.get("/linkedin/auth")
async def linkedin_auth():
    """Redirect browser to LinkedIn OAuth authorization page."""
    client_id, _ = _get_linkedin_creds()
    if not client_id:
        return JSONResponse({"error": "LINKEDIN_CLIENT_ID not configured"}, status_code=500)
    state = _secrets.token_urlsafe(16)
    _linkedin_oauth_states.add(state)
    params = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _LINKEDIN_REDIRECT_URI,
        "scope": _LINKEDIN_SCOPES,
        "state": state,
    })
    return RedirectResponse(f"{_LINKEDIN_AUTH_URL}?{params}")


@app.get("/linkedin/callback")
async def linkedin_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    """Handle LinkedIn OAuth callback — exchange code for tokens and store them."""
    if error:
        return JSONResponse({"error": f"LinkedIn auth error: {error}"}, status_code=400)
    if state not in _linkedin_oauth_states:
        return JSONResponse({"error": "Invalid or expired OAuth state"}, status_code=400)
    _linkedin_oauth_states.discard(state)

    client_id, client_secret = _get_linkedin_creds()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            _LINKEDIN_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": _LINKEDIN_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                return JSONResponse({"error": f"Token exchange failed: {text}"}, status_code=400)
            token_data = await resp.json()

    access_token = token_data["access_token"]

    async with aiohttp.ClientSession() as session:
        async with session.get(
            _LINKEDIN_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            userinfo = await resp.json()

    now = int(time.time())
    token_file = {
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": now + token_data.get("expires_in", 5184000),
        "refresh_token_expires_at": now + token_data.get("refresh_token_expires_in", 31536000),
        "user_id": userinfo.get("sub"),
        "name": userinfo.get("name"),
        "client_id": client_id,
        "client_secret": client_secret,
    }

    _LINKEDIN_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LINKEDIN_TOKEN_PATH.write_text(json.dumps(token_file, indent=2))
    log.info("LinkedIn tokens stored for user: %s", token_file.get("name"))

    return {"ok": True, "message": f"LinkedIn authorized for {token_file['name']}. You can close this tab."}


# ---------------------------------------------------------------------------
# HITL (Human-in-the-Loop) approval endpoints
# ---------------------------------------------------------------------------
def _get_telegram_token() -> str | None:
    """Get Telegram bot token — keyring first, env fallback."""
    try:
        import keyring
        token = keyring.get_password("hive-mind", "TELEGRAM_BOT_TOKEN")
        if token:
            return token
    except Exception:
        pass
    return os.getenv("TELEGRAM_BOT_TOKEN")


async def _send_telegram_approval_request(token: str, summary: str):
    """Send HITL approval DM to the owner via Telegram Bot API with inline keyboard."""
    bot_token = _get_telegram_token()
    chat_id = config.telegram_owner_chat_id

    if not bot_token or not chat_id:
        log.error("HITL: cannot send Telegram DM — missing bot token or owner chat ID")
        return

    safe_summary = summary[:4000].replace("<", "&lt;").replace(">", "&gt;")
    text = f"\U0001f514 Approval Required\n\n{safe_summary}"

    reply_markup = {
        "inline_keyboard": [
            [{"text": "\u2705 Approve", "callback_data": f"hitl_approve_{token}"}],
            [{"text": "\u274c Reject", "callback_data": f"hitl_deny_{token}"}],
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "reply_markup": reply_markup},
            ) as resp:
                data = await resp.json()
                message_id = data.get("result", {}).get("message_id")
                if message_id:
                    _hitl_messages[token] = (chat_id, message_id, text)
    except Exception:
        log.exception("HITL: failed to send Telegram DM")


async def _edit_hitl_message(token: str, status: str):
    """Edit a tracked HITL Telegram message to show a status and remove buttons.

    Args:
        token: The HITL token identifying the message.
        status: Status label to prepend (e.g. "Approved", "Denied", "Expired").
    """
    msg_info = _hitl_messages.pop(token, None)
    if msg_info is None:
        return

    chat_id, message_id, original_text = msg_info
    bot_token = _get_telegram_token()
    if not bot_token:
        return

    # Build status-prefixed text
    status_icons = {"Approved": "\u2705", "Denied": "\u274c", "Expired": "\u23f0"}
    icon = status_icons.get(status, "")
    # Replace the original header with the status
    new_text = f"{icon} {status}\n\n" + original_text.split("\n\n", 1)[-1]

    try:
        async with aiohttp.ClientSession() as session:
            # Update the message text and remove inline keyboard in a single call
            await session.post(
                f"https://api.telegram.org/bot{bot_token}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": new_text,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
    except Exception:
        log.exception("HITL: failed to edit Telegram message for token %s", token)


class HITLRequest(BaseModel):
    action: str
    summary: str
    ttl: int = DEFAULT_TTL
    wait: bool = True  # False = return token immediately (non-blocking)


class HITLResponse(BaseModel):
    token: str
    approved: bool


@app.post("/hitl/request")
async def hitl_request(body: HITLRequest):
    """Create an HITL approval request.

    With wait=True (default): blocks until approved, denied, or timeout.
    With wait=False: returns the token immediately for polling via GET /hitl/status/{token}.
    """
    ttl = max(30, min(body.ttl, 600))  # clamp: 30s–10min
    token, entry = hitl_store.create(body.action, body.summary, ttl=ttl)

    # Fire-and-forget: send Telegram DM to owner
    asyncio.create_task(_send_telegram_approval_request(token, body.summary))

    if not body.wait:
        return {"token": token, "state": "pending"}

    # Block until resolved or timeout
    try:
        await asyncio.wait_for(entry.event.wait(), timeout=ttl)
    except asyncio.TimeoutError:
        hitl_store.cleanup_expired()

    approved = entry.approved is True
    return {"approved": approved}


@app.get("/hitl/status/{token}")
async def hitl_status(token: str):
    """Poll the status of an HITL request. Returns state: pending|approved|denied|expired|unknown."""
    return hitl_store.status(token)


@app.post("/hitl/respond")
async def hitl_respond(
    body: HITLResponse,
    x_hitl_internal: str = Header(None),
):
    """Resolve an HITL approval request. Called by the Telegram bot."""
    if not config.hitl_internal_token:
        return JSONResponse({"error": "HITL not configured"}, status_code=500)

    if x_hitl_internal != config.hitl_internal_token:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ok = hitl_store.resolve(body.token, body.approved)
    if not ok:
        return JSONResponse({"error": "invalid or expired token"}, status_code=404)

    return {"ok": True}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.server_port)
