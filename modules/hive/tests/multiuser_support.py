"""Servers the multi-user tests talk to over real HTTP.

Two processes stand outside the console and are run here rather than
impersonated: comms (where a mind's owner is held) and auth-service (one of
the two account stores). Comms is a small stand-in served by uvicorn, because
what the console does with its answers is the thing under test and its own
code is not. auth-service is the real app, exported from its repository at a
pinned ref (`AUTH_SERVICE_REF`, default `main`) and run in its own process: its modules are named
`auth`, `config` and `server`, which would collide with the console's own on
one import path.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tarfile
import threading
import time
from io import BytesIO
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

AUTH_SERVICE_REPO = Path(
    os.getenv("AUTH_SERVICE_REPO", "/home/daniel/Storage/Dev/auth-service")
)
AUTH_SERVICE_REF = os.getenv("AUTH_SERVICE_REF", "main")
AUTH_SERVICE_ADMIN_TOKEN = "test-auth-service-admin-token"  # secret-guard: allow
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
    {"id": "m-skippy", "name": "skippy", "owner": "daniel"},
    {"id": "m-arnold", "name": "Arnold", "owner": "sloan"},
    {"id": "m-zack", "name": "zack", "owner": "aiden"},
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

    @app.get("/sessions/{session_id}")
    async def session(session_id: str):
        for row in SESSIONS:
            if row["id"] == session_id:
                return row
        return JSONResponse({"error": "Session not found"}, status_code=404)

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
    return app


class FakeComms(ThreadedServer):
    def __init__(self):
        app = fake_comms_app()
        super().__init__(app)
        app.state.base["url"] = self.url
        # Every write it was sent: what the console asked of comms, as it went.
        self.received = app.state.received


# ── auth-service ────────────────────────────────────────────────────────


def auth_service_ref_available() -> bool:
    result = subprocess.run(
        ["git", "-C", str(AUTH_SERVICE_REPO), "rev-parse", "--verify", "--quiet",
         f"{AUTH_SERVICE_REF}^{{commit}}"],
        capture_output=True,
    )
    return result.returncode == 0


def export_auth_service(destination: Path) -> Path:
    """The branch's tree, read out of git without touching that checkout."""
    archive = subprocess.run(
        ["git", "-C", str(AUTH_SERVICE_REPO), "archive", "--format=tar", AUTH_SERVICE_REF],
        capture_output=True,
        check=True,
    ).stdout
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    return destination


class AuthServiceProcess:
    """The real auth-service on a free port, with its own throwaway db."""

    def __init__(self, source: Path, db_path: Path):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._source = source
        self._db_path = db_path
        self._process: subprocess.Popen | None = None

    def __enter__(self):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "AUTH_DB": str(self._db_path),
            "AUTH_HOST": "127.0.0.1",
            "AUTH_PORT": str(self.port),
            "AUTH_COOKIE_SECURE": "false",
            "AUTH_COOKIE_DOMAIN": "",
            "AUTH_SEED_USERNAME": SEED_USERNAME,
            "AUTH_SEED_PASSWORD": SEED_PASSWORD,
            "AUTH_SERVICE_ADMIN_TOKEN": AUTH_SERVICE_ADMIN_TOKEN,
            "AUTH_RESET_EMAIL": "",
            "SMTP_HOST": "",
        }
        self._process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=self._source,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 20
        while True:
            if self._process.poll() is not None:
                raise RuntimeError(
                    "auth-service exited: " + self._process.stderr.read().decode()[-2000:]
                )
            try:
                if httpx.get(f"{self.url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("auth-service did not start")
            time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def admin(self, method: str, path: str, **kwargs) -> httpx.Response:
        return httpx.request(
            method,
            f"{self.url}{path}",
            headers={"Authorization": f"Bearer {AUTH_SERVICE_ADMIN_TOKEN}"},
            timeout=10,
            **kwargs,
        )
