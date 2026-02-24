"""
Hive Mind — FastAPI gateway server.

Thin HTTP/WebSocket layer over the session manager.
All Claude CLI interaction flows through here.
"""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import config
from models import ModelRegistry, Provider
from sessions import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hive-mind.server")

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
    log.info("Gateway started on port %d", config.server_port)
    yield
    await session_mgr.shutdown()


app = FastAPI(title="Hive Mind Gateway", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class CreateSessionRequest(BaseModel):
    owner_type: str
    owner_ref: str
    client_ref: str
    model: str | None = None


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
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.server_port)
