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
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from comms.config import PROJECT_DIR, config
import comms.broker as broker
from comms.auth import require_admin_bearer, require_bearer
from comms.broker import check_secret_scope, get_secret_scopes, grant_secret_scope, revoke_secret_scope
from comms.models import ModelRegistry, Provider
from comms.network_identity import resolve_container_name
from comms.secrets import get_credential
from comms.sessions import SessionManager

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
_KEYRING_ENV_KEYS = ["MCP_AUTH_TOKEN", "GITHUB_TOKEN"]

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await session_mgr.start()

    # Broker DB init + startup recovery. broker.minds IS the mind
    # registry — there is no in-memory cache. Every lookup queries the
    # DB directly. Single source of truth.
    _broker_db_path = os.environ.get("BROKER_DB_PATH", str(PROJECT_DIR / "data" / "broker.db"))
    Path(_broker_db_path).parent.mkdir(parents=True, exist_ok=True)
    app.state.broker_db = await broker.init_db(_broker_db_path)
    session_mgr.broker_db = app.state.broker_db

    pending = await broker.recover_stranded_messages(app.state.broker_db)
    for msg in pending:
        asyncio.create_task(broker.wakeup_and_collect(
            app.state.broker_db, session_mgr,
            message_id=msg["id"],
            conversation_id=msg["conversation_id"],
            from_mind=msg["from_mind"],
            to_mind=msg["to_mind"],
            content=msg["content"],
            rolling_summary=msg["rolling_summary"] or "",
            message_number=msg["message_number"],
            metadata=json.loads(msg["metadata"]) if msg.get("metadata") else None,
        ))

    log.info("Gateway started on port %d", config.server_port)
    yield
    await app.state.broker_db.close()
    await session_mgr.shutdown()


app = FastAPI(
    title="Hive Mind Gateway",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def _bearer_gate(request: Request, call_next):
    """HTTP-only bearer gate, equivalent to the old app-level `Depends(require_bearer)`.

    A `Request`-typed FastAPI dependency can't be applied globally on this
    app: FastAPI tries to run it for the WebSocket routes too, and a
    WebSocket connection has no `Request` to build, so dependency
    resolution throws before the handler ever runs. `@app.middleware("http")`
    only wraps the HTTP scope — WebSocket connections bypass it entirely —
    so the two `/sessions/{id}/stream` and `/sessions/{id}/attach` routes
    keep doing their own manual bearer check, same as they always have.
    """
    try:
        require_bearer(request, request.headers.get("authorization", ""))
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Gated by the regular bearer (set the token on the
    Docker healthcheck command line). Returns 200 once the app has booted.
    """
    return {"status": "ok", "service": "hive-comms"}


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
    mind_id: str


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


class RotationMemoryRequest(BaseModel):
    """Written by the per-mind rotation_check hook on Stop events.

    The hook summarizes the just-completed session transcript and POSTs
    the body here. comms persists it in session_memory, keyed by
    (mind_id, client_ref). The next session-create for that
    (mind_id, client_ref) loads it as the <session-memory> carry-forward
    block via bootstrap_loader.
    """
    mind_id: str
    client_ref: str
    body: str


class BrokerMessageRequest(BaseModel):
    message_id: str | None = None
    conversation_id: str
    from_mind: str = Field(alias="from")
    to_mind: str = Field(alias="to")
    content: str
    rolling_summary: str = ""
    metadata: dict | None = None

    model_config = ConfigDict(populate_by_name=True)


class BrokerMessageResponse(BaseModel):
    status: str
    conversation_id: str
    message_id: str


class RegisterMindRequest(BaseModel):
    mind_id: str  # UUID — durable routing key
    name: str     # display label
    gateway_url: str
    model: str
    harness: str


class UpdateMindRequest(BaseModel):
    gateway_url: str | None = None
    model: str | None = None
    harness: str | None = None


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------
@app.post("/sessions")
async def create_session(body: CreateSessionRequest):
    # Single-mind mode: restrict to the configured mind
    try:
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
    except ConnectionError:
        return JSONResponse(
            {"mind_id": body.mind_id, "error": "mind_unreachable"},
            status_code=503,
        )


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


@app.get("/sessions/late-turns")
async def late_turns(client_type: str, client_ref: str, since: float):
    """Return the active session's turns committed after a watermark.

    The rotation Stop hook calls this just before ``/clear`` to merge any
    turn that landed during its Ollama window — the durable late-turn
    handoff source of truth, independent of transcript timing. Declared
    before ``/sessions/{session_id}`` so the literal path wins the match.
    """
    return await session_mgr.get_late_turns(client_type, client_ref, since)


class ArmRotationRequest(BaseModel):
    client_type: str
    client_ref: str


@app.post("/sessions/arm-rotation")
async def arm_rotation(body: ArmRotationRequest):
    """Arm the active session for pending rotation (finalize-on-user-turn).

    Called by the ``rotation_check`` Stop hook after it writes the
    carry-forward, in place of an inline ``/clear``. The actual session swap
    happens on the next user turn in ``send_message``. Declared before
    ``/sessions/{session_id}`` so the literal path wins the match.
    """
    return await session_mgr.arm_rotation(body.client_type, body.client_ref)


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


@app.get("/sessions/{session_id}/events")
async def stream_session_events(session_id: str):
    """Read-only SSE stream for passive session observers."""
    session = await session_mgr.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    async def event_stream():
        async for event in session_mgr.stream_session_events(session_id):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str):
    """Return the human/assistant turn history for a session from the session_turns table."""
    messages = await session_mgr.get_session_turns(session_id)
    return {"session_id": session_id, "messages": messages}


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
# Rotation-memory endpoint
# ---------------------------------------------------------------------------
@app.post("/sessions/{session_id}/rotation-memory")
async def write_rotation_memory(session_id: str, body: RotationMemoryRequest):
    """Persist a rotation summary for (mind_id, client_ref).

    Written by the per-mind ``rotation_check`` Stop hook when the
    transcript crosses the rotation threshold. The next
    ``create_session`` for the same (mind_id, client_ref) reads this row
    via ``bootstrap_loader._fetch_session_memory`` and injects it as the
    ``<session-memory>`` carry-forward block.
    """
    if not body.body.strip():
        return JSONResponse({"error": "body required"}, status_code=400)
    db = session_mgr._db
    if db is None:
        return JSONResponse({"error": "session manager not started"}, status_code=503)
    # Resolve mind_name from broker.minds for symmetry with create_session.
    row = await broker.get_mind_by_id(app.state.broker_db, body.mind_id)
    mind_name = (row or {}).get("name") or body.mind_id
    cursor = await db.execute(
        """INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at)
           VALUES (?, ?, ?, ?, ?, strftime('%s','now'))""",
        (body.mind_id, mind_name, body.client_ref, session_id, body.body),
    )
    await db.commit()
    return {"ok": True, "id": cursor.lastrowid}


# ---------------------------------------------------------------------------
# Interrupt endpoint
# ---------------------------------------------------------------------------
@app.post("/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    """Send SIGINT to the running subprocess without killing the session."""
    try:
        result = await session_mgr.interrupt_session(session_id)
        return result
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


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
    # FastAPI dependencies don't apply to WebSocket routes — gate manually.
    expected = os.environ.get("COMMS_BEARER_TOKEN", "")
    admin = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    if expected:
        auth = ws.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if token != expected and (not admin or token != admin):
            await ws.close(code=4401)
            return
    await ws.accept()
    try:
        while True:
            data = await ws.receive_json()
            images = data.get("images")
            async for event in session_mgr.send_message(session_id, data["content"], images=images):
                await ws.send_json(event)
    except WebSocketDisconnect:
        pass


async def _pump_attach_ws(browser_ws: WebSocket, mind_ws) -> int | None:
    """Bridge the browser's WS and the mind's pty-attach WS.

    Frame types are load-bearing on the browser→mind leg: BINARY frames
    are raw terminal bytes, TEXT frames are JSON control messages
    (resize) the mind applies to the pty — so TEXT must be forwarded as
    TEXT, never re-encoded into the byte stream. Whichever side closes
    first ends the bridge — a live pty and a browser tab have no
    independent life of their own once either end is gone.

    Returns the mind's close code when the mind is the end that closed, so
    the caller can pass it through to the browser. That code is
    the browser's only way to tell a socket it should stand down from one
    it should reconnect: a mind evicting a stale attach closes 1012, which
    tells that tile another window has the keyboard. Swallowed, it reads as
    a dropped connection, both tiles reconnect, and each eviction starts
    the next one — a tug-of-war that repaints both terminals about once a
    second until a tab is closed.
    """
    upstream_close: int | None = None

    async def browser_to_mind() -> None:
        while True:
            msg = await browser_ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("bytes"):
                await mind_ws.send_bytes(msg["bytes"])
            elif msg.get("text"):
                await mind_ws.send_str(msg["text"])

    async def mind_to_browser() -> None:
        nonlocal upstream_close
        async for msg in mind_ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await browser_ws.send_bytes(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                await browser_ws.send_bytes(msg.data.encode())
            else:  # ERROR, and anything else the iterator does surface
                break
        # aiohttp's async iterator does not hand CLOSE frames to the loop —
        # it stops on them — so the code is read off the socket afterwards,
        # not out of a message. Iterating for it instead silently loses
        # every close code the mind sends.
        code = mind_ws.close_code
        if isinstance(code, int):
            upstream_close = code

    tasks = [asyncio.ensure_future(browser_to_mind()), asyncio.ensure_future(mind_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
    return upstream_close


@app.websocket("/sessions/{session_id}/attach")
async def ws_attach(ws: WebSocket, session_id: str):
    """Reverse-proxy a browser terminal WS into the owning mind's pty attach.

    Resolves the session's mind via the DB (not the in-memory `_procs` cache,
    which is empty after a restart even though the session and its container
    are both still alive) so a fresh hive-comms process can still route an
    attach. Session-lifecycle knowledge (claude_sid, model) stays here;
    the mind's attach-pty route stays stateless per-request.
    """
    expected = os.environ.get("COMMS_BEARER_TOKEN", "")
    admin = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    if expected:
        auth = ws.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if token != expected and (not admin or token != admin):
            await ws.close(code=4401)
            return

    session = await session_mgr.get_session(session_id)
    if not session:
        await ws.close(code=4404, reason=f"session {session_id} not found")
        return
    try:
        mind_row = await session_mgr._get_mind_row(session["mind_id"])
    except ValueError as exc:
        await ws.close(code=4404, reason=str(exc))
        return

    # A session without a conversation id cannot be attached to. Say so
    # instead of proxying through and letting the terminal open on nothing —
    # a blank tile beside a live chat is worse than a visible failure.
    conversation_id = (session.get("claude_sid") or "").strip()
    if not conversation_id:
        log.error("session %s has no conversation id — refusing attach", session_id)
        await ws.close(code=4409, reason="session has no conversation to attach to")
        return

    mind_ws_url = mind_row["gateway_url"].replace("http://", "ws://").replace("https://", "wss://")
    params = urlencode({
        "resume_sid": conversation_id,
        "model": session.get("model") or "sonnet",
        # Initial pty geometry from the browser tile, so the TUI's first
        # paint matches; live changes arrive as resize control frames.
        "cols": ws.query_params.get("cols") or "80",
        "rows": ws.query_params.get("rows") or "24",
    })
    attach_url = f"{mind_ws_url}/sessions/{session_id}/attach-pty?{params}"

    await ws.accept()

    async def _watch_for_close() -> None:
        # The attach pty is a separate process from the session's tracked
        # subprocess — kill_session never reaches it. Watching the session
        # event stream lets a kill (end-session, /kill, rotation) tear the
        # bridge down, which cascades: mind_ws closes → mind_server's
        # attach loop exits → the pty process is terminated.
        async for event in session_mgr.stream_session_events(session_id):
            if event.get("type") == "session_closed":
                return

    # Attaching to an already-closed (archived) session is a deliberate
    # resurrection — no close-watcher, or it would fire instantly. The
    # watcher only guards sessions that were live at attach time.
    was_live = session.get("status") != "closed"

    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(attach_url) as mind_ws:
                pump = asyncio.ensure_future(_pump_attach_ws(ws, mind_ws))
                tasks = {pump}
                closed = None
                if was_live:
                    closed = asyncio.ensure_future(_watch_for_close())
                    tasks.add(closed)
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                if closed is not None and closed in done:
                    await ws.close(code=4410, reason="session closed")
                elif pump in done:
                    upstream = pump.result()
                    if upstream is not None:
                        await ws.close(code=upstream)
    except aiohttp.WSServerHandshakeError as exc:
        # The mind answered, and said no. Starlette replies 403 to a
        # websocket path it has no route for, so this is how a mind whose
        # image predates the pty work presents: reachable, no terminal.
        # That is permanent until the mind is rebuilt, and a client that
        # cannot tell it apart from a dropped connection will reconnect
        # forever — so it gets its own close code and no retry.
        log.warning("attach-pty proxy to %s refused: %s", attach_url, exc)
        await ws.close(code=4415, reason="mind has no terminal attach route")
    except aiohttp.ClientError as exc:
        log.warning("attach-pty proxy to %s failed: %s", attach_url, exc)
        await ws.close(code=1011, reason="mind unreachable")


# ---------------------------------------------------------------------------
# Slash command routing (used by clients)
# ---------------------------------------------------------------------------
SERVER_COMMANDS = {"/clear", "/model", "/autopilot", "/kill", "/prune", "/status", "/sessions", "/switch", "/new", "/remember"}


class CommandRequest(BaseModel):
    content: str
    owner_type: str = "terminal"
    owner_ref: str = ""
    client_ref: str = ""
    mind_id: str


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


async def _selectable(body: CommandRequest) -> list[dict]:
    """The one list every command numbers against.

    /sessions, /switch and /kill all take positions in it, so they have to
    be reading the same list — numbering them separately is how "/switch 3"
    lands on someone else's 3.
    """
    return await session_mgr.list_selectable_sessions(
        owner_ref=body.owner_ref,
        client_type=body.owner_type,
        client_ref=body.client_ref,
        mind_id=body.mind_id,
    )


async def _resolve_target(target: str, body: CommandRequest) -> tuple[str, dict | None]:
    """A session id, or a position in the list the user was just shown."""
    if not target.isdigit():
        return target, None
    sessions = await _selectable(body)
    idx = int(target) - 1
    if 0 <= idx < len(sessions):
        return sessions[idx]["id"], None
    return target, {"error": f"Invalid session number: {target}"}


async def _handle_command(cmd: str, parts: list[str], body: CommandRequest):

    if cmd == "/status":
        sessions = await session_mgr.list_sessions()
        running = sum(1 for s in sessions if s["status"] == "running")
        return {
            "server_port": config.server_port,
            "total_sessions": len(sessions),
            "running_sessions": running,
        }

    if cmd == "/sessions":
        return await _selectable(body)

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
            mind_id=body.mind_id,
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
        target, error = await _resolve_target(parts[1], body)
        if error:
            return error
        return await session_mgr.activate_session(
            target, body.owner_type, body.client_ref,
            owner_type=body.owner_type, owner_ref=body.owner_ref,
        )

    if cmd == "/kill":
        if len(parts) < 2:
            return {"error": "Usage: /kill <session_id or number>"}
        target, error = await _resolve_target(parts[1], body)
        if error:
            return error
        return await session_mgr.kill_session(target)

    if cmd == "/prune":
        active = await session_mgr.get_active_session(body.owner_type, body.client_ref)
        active_id = active["id"] if active else None
        sessions = await session_mgr.list_sessions(owner_ref=body.owner_ref)
        killed: list[str] = []
        for s in sessions:
            if s["id"] == active_id:
                continue
            try:
                await session_mgr.kill_session(s["id"])
                killed.append(s["id"])
            except Exception:
                pass
        return {"killed": killed, "kept": active_id}

    if cmd == "/remember":
        return {
            "response": (
                "The memory pipeline runs automatically when you start a new session with /new. "
                "To save something specific right now, say 'remember this' in the conversation."
            )
        }

    return {"error": f"Unknown command: {cmd}"}


# ---------------------------------------------------------------------------
# Broker endpoints (inter-mind messaging)
# ---------------------------------------------------------------------------
async def _mind_exists(mind_id: str) -> bool:
    """Check if a mind is registered in the broker."""
    return await broker.get_mind_by_id(app.state.broker_db, mind_id) is not None


@app.get("/broker/minds")
async def broker_get_minds():
    """Return all registered minds from the broker database."""
    db = app.state.broker_db
    return await broker.get_registered_minds(db)


@app.post("/broker/minds", dependencies=[Depends(require_admin_bearer)])
async def broker_register_mind(body: RegisterMindRequest):
    """Register (or update) a mind in the broker database. Admin-only."""
    db = app.state.broker_db
    await broker.register_mind(
        db,
        mind_id=body.mind_id,
        name=body.name,
        gateway_url=body.gateway_url,
        model=body.model,
        harness=body.harness,
    )
    return await broker.get_mind_by_id(db, body.mind_id)


@app.put("/broker/minds/{name}", dependencies=[Depends(require_admin_bearer)])
async def broker_update_mind(name: str, body: UpdateMindRequest):
    """Partially update a mind's fields. Admin-only."""
    db = app.state.broker_db
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    result = await broker.update_mind(db, name, **fields)
    if result is None:
        return JSONResponse({"error": f"Mind '{name}' not found"}, status_code=404)
    return result


@app.delete("/broker/minds/{name}", dependencies=[Depends(require_admin_bearer)])
async def broker_delete_mind(name: str):
    """Deregister a mind from the broker database. Admin-only."""
    db = app.state.broker_db
    deleted = await broker.delete_mind(db, name)
    if not deleted:
        return JSONResponse({"error": f"Mind '{name}' not found"}, status_code=404)
    return {"ok": True, "name": name}


@app.post("/broker/messages", response_model=BrokerMessageResponse)
async def broker_post_message(body: BrokerMessageRequest):
    """Receive an inter-mind message, write to DB, kick off background wakeup."""
    if not await _mind_exists(body.to_mind):
        return JSONResponse(
            {"error": f"Mind '{body.to_mind}' not found in broker.minds. Register via POST /broker/minds."},
            status_code=404,
        )

    db = app.state.broker_db
    message_id = body.message_id or str(uuid.uuid4())
    message_number = await broker.get_next_message_number(db, body.conversation_id)
    metadata = body.metadata

    result = await broker.insert_message(
        db,
        message_id=message_id,
        conversation_id=body.conversation_id,
        from_mind=body.from_mind,
        to_mind=body.to_mind,
        message_number=message_number,
        content=body.content,
        rolling_summary=body.rolling_summary,
        metadata=metadata,
        status="pending",
    )

    if result.get("existing"):
        return BrokerMessageResponse(
            status="exists",
            conversation_id=body.conversation_id,
            message_id=message_id,
        )

    asyncio.create_task(broker.wakeup_and_collect(
        db, session_mgr,
        message_id=message_id,
        conversation_id=body.conversation_id,
        from_mind=body.from_mind,
        to_mind=body.to_mind,
        content=body.content,
        rolling_summary=body.rolling_summary,
        message_number=message_number,
        metadata=metadata,
    ))

    return BrokerMessageResponse(
        status="dispatched",
        conversation_id=body.conversation_id,
        message_id=message_id,
    )


@app.get("/broker/messages")
async def broker_get_messages(conversation_id: str):
    """Get all messages for a conversation."""
    db = app.state.broker_db
    messages = await broker.get_messages(db, conversation_id)
    return messages


@app.get("/broker/conversations/{conversation_id}")
async def broker_get_conversation(conversation_id: str):
    """Get conversation detail with all messages."""
    db = app.state.broker_db
    messages = await broker.get_messages(db, conversation_id)
    if not messages:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)
    return {"conversation_id": conversation_id, "messages": messages}


# ---------------------------------------------------------------------------
# Secrets API — network-identity-based secret access for mind containers
# ---------------------------------------------------------------------------
class SecretScopeRequest(BaseModel):
    mind_name: str
    secret_keys: list[str]  # keys this mind is allowed to access


@app.get("/secrets/{key}")
async def secrets_get(key: str, request: Request):
    """Return a secret value to an identified and scoped mind container.

    Identifies the caller by Docker network reverse DNS (source IP).
    Checks the secret_scopes table for authorization.
    """
    # 1. Identify caller by source IP
    caller_ip = request.client.host if request.client else None
    if not caller_ip:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    mind_name = await resolve_container_name(caller_ip)
    if mind_name is None:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    # 2. Check scope
    db = app.state.broker_db
    allowed = await check_secret_scope(db, mind_name, key)
    if not allowed:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    # 3. Retrieve secret
    value = get_credential(key)
    if value is None:
        return JSONResponse({"error": "secret not found"}, status_code=404)

    return {"key": key, "value": value}


@app.post("/secrets/scopes", dependencies=[Depends(require_admin_bearer)])
async def secrets_grant_scopes(body: SecretScopeRequest):
    """Grant a mind access to one or more secret keys. Admin-only."""
    db = app.state.broker_db
    for key in body.secret_keys:
        await grant_secret_scope(db, body.mind_name, key)
    return {"ok": True, "mind_name": body.mind_name, "granted": body.secret_keys}


@app.delete("/secrets/scopes", dependencies=[Depends(require_admin_bearer)])
async def secrets_revoke_scopes(body: SecretScopeRequest):
    """Revoke a mind's access to one or more secret keys. Admin-only."""
    db = app.state.broker_db
    for key in body.secret_keys:
        await revoke_secret_scope(db, body.mind_name, key)
    return {"ok": True, "mind_name": body.mind_name, "revoked": body.secret_keys}


@app.get("/secrets/scopes/{mind_name}")
async def secrets_list_scopes(mind_name: str, request: Request):
    """List all secret keys a mind is allowed to access.

    Auth paths:
    - Admin bearer (cross-mind listing — caller doesn't have to match mind_name)
    - Network identity (a mind listing its own scopes — caller IP resolves to mind_name)

    The router-level `require_bearer` is already satisfied at this point;
    here we just decide whether the caller can read THIS specific mind's scopes.
    """
    admin_token = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    auth_header = request.headers.get("authorization", "")
    is_admin = (
        admin_token
        and auth_header.startswith("Bearer ")
        and auth_header[7:] == admin_token
    )

    if not is_admin:
        caller_ip = request.client.host if request.client else None
        if not caller_ip:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        caller_name = await resolve_container_name(caller_ip)
        if caller_name != mind_name:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    db = app.state.broker_db
    keys = await get_secret_scopes(db, mind_name)
    return {"mind_name": mind_name, "secret_keys": keys}


# ---------------------------------------------------------------------------
# LinkedIn OAuth
# ---------------------------------------------------------------------------
_LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
_LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
_LINKEDIN_REDIRECT_URI = config.linkedin.get("redirect_uri", "") if hasattr(config, "linkedin") else os.environ.get("LINKEDIN_REDIRECT_URI", "")
_LINKEDIN_SCOPES = "openid profile email w_member_social"
_LINKEDIN_TOKEN_PATH = Path(os.environ.get("LINKEDIN_TOKEN_PATH", "credentials/linkedin_token.json"))

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
# SMS inbound webhook (sms-gate.app local-server mode)
# ---------------------------------------------------------------------------
from comms.sms_inbound import (
    build_broker_message_id,
    extract_message_fields,
    format_dispatch_content,
    verify_signature,
)


SMS_INBOUND_ENABLED_KEY = "sms_inbound_enabled"


async def _sms_inbound_enabled() -> bool:
    """Read the toggle flag. Default False — opt-in, off by default."""
    value = await broker.get_setting(
        app.state.broker_db, SMS_INBOUND_ENABLED_KEY, default="false"
    )
    return value == "true"


@app.get("/sms/inbound/enabled")
async def get_sms_inbound_enabled():
    """Report whether inbound SMS processing is active."""
    return {"enabled": await _sms_inbound_enabled()}


@app.put("/sms/inbound/enabled")
async def set_sms_inbound_enabled(body: dict):
    """Toggle inbound SMS processing on or off."""
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        raise HTTPException(400, "body must be {\"enabled\": bool}")
    await broker.set_setting(
        app.state.broker_db,
        SMS_INBOUND_ENABLED_KEY,
        "true" if body["enabled"] else "false",
    )
    return {"enabled": body["enabled"]}


@app.post("/sms/inbound")
async def sms_inbound(request: Request):
    """Receive an SMS/MMS webhook from the sms-gate.app Android client.

    Verifies HMAC-SHA256 against `SMS_INBOUND_HMAC_SECRET`, extracts the
    message fields permissively (the public docs are stale on
    `mms:downloaded`), and dispatches a broker message to Ada keyed on the
    sender phone number so back-and-forth threads to one conversation.

    Honors the `sms_inbound_enabled` setting — when off, returns 200 with
    `{"status": "disabled"}` so sms-gate.app doesn't retry, but skips
    HMAC verification, broker insertion, and dispatch entirely.
    """
    if not await _sms_inbound_enabled():
        return {"status": "disabled"}

    secret = os.environ.get("SMS_INBOUND_HMAC_SECRET", "").strip()
    if not secret:
        log.error("sms/inbound: SMS_INBOUND_HMAC_SECRET not configured")
        return JSONResponse({"error": "server misconfigured"}, status_code=500)

    body = await request.body()
    signature = request.headers.get("x-signature", "")
    timestamp = request.headers.get("x-timestamp", "")

    if not verify_signature(body, timestamp, signature, secret):
        log.warning(
            "sms/inbound: HMAC verification failed sig_present=%s ts_present=%s body_bytes=%d",
            bool(signature), bool(timestamp), len(body),
        )
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log.warning("sms/inbound: invalid JSON body: %s", e)
        return JSONResponse({"error": "invalid json"}, status_code=400)

    log.info("sms/inbound: payload %s", payload)
    fields = extract_message_fields(payload)
    log.info(
        "sms/inbound: event=%s sender=%s text=%r message_id=%s",
        fields.get("event"), fields.get("sender"), fields.get("text"), fields.get("message_id"),
    )

    ada = await broker.get_mind(app.state.broker_db, "ada")
    if not ada:
        log.error("sms/inbound: ada not registered in broker.minds — dropping")
        return JSONResponse({"status": "ack-no-recipient"}, status_code=200)

    sender = fields.get("sender") or "unknown"
    conversation_id = f"sms-{sender}"
    message_id = build_broker_message_id(sender, fields.get("message_id"))
    content = format_dispatch_content(sender, fields.get("text"))

    message_number = await broker.get_next_message_number(app.state.broker_db, conversation_id)
    insert_result = await broker.insert_message(
        app.state.broker_db,
        message_id=message_id,
        conversation_id=conversation_id,
        from_mind="sms-gateway",
        to_mind=ada["id"],
        message_number=message_number,
        content=content,
        rolling_summary="",
        metadata={
            "source": "sms-inbound",
            "sender": sender,
            "gateway_message_id": fields.get("message_id"),
            "event": fields.get("event"),
            "received_at": fields.get("received_at"),
        },
        status="pending",
    )
    if insert_result.get("existing"):
        return {"status": "duplicate", "message_id": message_id}

    asyncio.create_task(broker.wakeup_and_collect(
        app.state.broker_db, session_mgr,
        message_id=message_id,
        conversation_id=conversation_id,
        from_mind="sms-gateway",
        to_mind=ada["id"],
        content=content,
        rolling_summary="",
        message_number=message_number,
        metadata={"source": "sms-inbound", "sender": sender},
    ))

    return {"status": "dispatched", "message_id": message_id}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.server_port)
