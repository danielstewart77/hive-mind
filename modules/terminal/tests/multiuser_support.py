"""A comms stand-in the multi-user tests talk to over real HTTP.

Comms (where a mind's owner is held) is a small stand-in served by uvicorn,
because what the site does with its answers is the thing under test and
comms' own code is not. Nothing else runs beside the site: the hive signs
people in itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

SEED_USERNAME = "daniel"
SEED_PASSWORD = "daniel-seed-password"  # secret-guard: allow


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ThreadedServer:
    """An ASGI app on a real port, for code that opens its own connections."""

    def __init__(self, app):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("stand-in server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=5)


# ── comms ───────────────────────────────────────────────────────────────

# Who owns what. `orphan` registered before owners existed and carries none.
MINDS = [
    {"id": "m-skippy", "name": "skippy", "owner": "daniel", "voice": "skippy-clip.wav"},
    {"id": "m-arnold", "name": "Arnold", "owner": "sloan", "voice": "arnold-clip.wav"},
    {"id": "m-zack", "name": "zack", "owner": "aiden", "voice": "zack-clip.wav"},
    {"id": "m-orphan", "name": "orphan"},
]
SESSIONS = [
    {"id": "s-skippy", "mind_id": "m-skippy", "status": "active"},
    {"id": "s-arnold", "mind_id": "m-arnold", "status": "active"},
    {"id": "s-zack", "mind_id": "m-zack", "status": "active"},
]


def fake_comms_app() -> FastAPI:
    """Comms' read routes, and every mind's own gateway, in one app.

    Each mind's `gateway_url` points back here, so a route the console relays
    to a mind gets an answer and the only refusals left are the console's.
    """
    app = FastAPI()
    base = {"url": ""}
    received: list[dict] = []

    @app.get("/broker/minds")
    async def minds():
        return [dict(m, gateway_url=base["url"]) for m in MINDS]

    @app.get("/sessions")
    async def sessions():
        return SESSIONS

    @app.get("/sessions/names")
    async def names():
        return {row["id"]: {"name": f"name of {row['id']}", "color": ""} for row in SESSIONS}

    @app.get("/sessions/{session_id}")
    async def session(session_id: str):
        for row in SESSIONS:
            if row["id"] == session_id:
                return row
        return JSONResponse({"error": "Session not found"}, status_code=404)

    @app.websocket("/sessions/{session_id}/attach")
    async def attach(websocket: WebSocket, session_id: str):
        # What a mind's pty would answer: proof the attach reached it, then
        # a pane that stays open, echoing, until the other end goes.
        await websocket.accept()
        await websocket.send_bytes(f"attached {session_id}".encode())
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                await websocket.send_bytes(message.get("bytes") or b"")

    @app.get("/sessions/{session_id}/events")
    async def events(session_id: str):
        # A session's event stream: one event, or an event every tenth of a
        # second for as long as a test has asked it to stay open.
        async def stream():
            n = 0
            deadline = time.monotonic() + app.state.stream["seconds"]
            while True:
                yield f"data: {{\"n\": {n}, \"session\": \"{session_id}\"}}\n\n"
                n += 1
                if time.monotonic() >= deadline:
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.api_route(
        "/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def anything(rest: str, request: Request):
        body = await request.body()
        received.append(
            {
                "method": request.method,
                "path": "/" + rest,
                "authorization": request.headers.get("authorization", ""),
                "body": body.decode("utf-8", "replace"),
            }
        )
        return {}

    app.state.base = base
    app.state.received = received
    app.state.stream = {"seconds": 0.0}
    return app


class FakeComms(ThreadedServer):
    def __init__(self):
        app = fake_comms_app()
        super().__init__(app)
        app.state.base["url"] = self.url
        # Every write it was sent: what the console asked of comms, as it went.
        self.received = app.state.received
        # How long a session's event stream stays open.
        self.stream = app.state.stream
