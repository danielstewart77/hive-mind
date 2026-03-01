"""
Hive Mind — FastAPI gateway server.

Thin HTTP/WebSocket layer over the session manager.
All Claude CLI interaction flows through here.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from config import config
from core.hitl import hitl_store, TOKEN_TTL
from core.models import ModelRegistry, Provider
from core.sessions import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hive-mind.server")

# ---------------------------------------------------------------------------
# Keyring → env bridge: expose keyring secrets as env vars so non-Python
# consumers (e.g. Claude Code reading .mcp.container.json) can resolve them.
# ---------------------------------------------------------------------------
_KEYRING_ENV_KEYS = ["MCP_AUTH_TOKEN", "HITL_INTERNAL_TOKEN"]

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
    """Periodically purge expired HITL tokens."""
    while True:
        await asyncio.sleep(30)
        hitl_store.cleanup_expired()


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


class MessageRequest(BaseModel):
    content: str


class ModelSwitchRequest(BaseModel):
    model: str


class ActivateRequest(BaseModel):
    client_type: str
    client_ref: str


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
    async def event_stream():
        async for event in session_mgr.send_message(session_id, body.content):
            yield f"data: {json.dumps(event)}\n\n"

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
# Model listing
# ---------------------------------------------------------------------------
@app.get("/models")
async def list_models():
    return await model_registry.list_models()


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/sessions/{session_id}/stream")
async def ws_stream(ws: WebSocket, session_id: str):
    await ws.accept()
    try:
        while True:
            data = await ws.receive_json()
            async for event in session_mgr.send_message(session_id, data["content"]):
                await ws.send_json(event)
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# Slash command routing (used by clients)
# ---------------------------------------------------------------------------
SERVER_COMMANDS = {"/clear", "/model", "/autopilot", "/kill", "/status", "/sessions", "/switch", "/new"}


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

    if cmd == "/new":
        return await session_mgr.create_session(
            owner_type=body.owner_type,
            owner_ref=body.owner_ref,
            client_ref=body.client_ref,
        )

    if cmd == "/clear":
        # Get active session, kill it, create new one
        active = await session_mgr.get_active_session(body.owner_type, body.client_ref)
        if active:
            await session_mgr.kill_session(active["id"])
        return await session_mgr.create_session(
            owner_type=body.owner_type,
            owner_ref=body.owner_ref,
            client_ref=body.client_ref,
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

    return {"error": f"Unknown command: {cmd}"}


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
    """Send HITL approval DM to the owner via Telegram Bot API."""
    bot_token = _get_telegram_token()
    chat_id = config.telegram_owner_chat_id

    if not bot_token or not chat_id:
        log.error("HITL: cannot send Telegram DM — missing bot token or owner chat ID")
        return

    # Truncate and escape summary for Telegram
    safe_summary = summary[:200].replace("<", "&lt;").replace(">", "&gt;")
    text = (
        f"Approval required:\n\n"
        f"{safe_summary}\n\n"
        f"✅ /approve_{token}\n\n"
        f"─────────────────\n\n"
        f"❌ /deny_{token}"
    )

    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
    except Exception:
        log.exception("HITL: failed to send Telegram DM")


class HITLRequest(BaseModel):
    action: str
    summary: str


class HITLResponse(BaseModel):
    token: str
    approved: bool


@app.post("/hitl/request")
async def hitl_request(body: HITLRequest):
    """Create an HITL approval request. Blocks until approved, denied, or timeout."""
    token, entry = hitl_store.create(body.action, body.summary)

    # Fire-and-forget: send Telegram DM to owner
    asyncio.create_task(_send_telegram_approval_request(token, body.summary))

    # Block until resolved or timeout
    try:
        await asyncio.wait_for(entry.event.wait(), timeout=TOKEN_TTL)
    except asyncio.TimeoutError:
        hitl_store.cleanup_expired()

    approved = entry.approved is True
    return {"approved": approved}


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
