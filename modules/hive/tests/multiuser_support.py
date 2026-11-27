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
            uvicorn.Config(app, host="0.0.0.0", port=self.port, log_level="warning")
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

    @app.middleware("http")
    async def _forward_route(request: Request, call_next):
        """The gateway's forward route (story 898): a request for a mind is
        answered as that mind's own route would answer it, and marked as the
        mind's answer so the console tells it from the gateway's own."""
        path = request.scope.get("path", "")
        marker = "/forward/"
        if path.startswith("/broker/minds/") and marker in path:
            request.scope["path"] = "/" + path.split(marker, 1)[1]
            request.scope["raw_path"] = request.scope["path"].encode()
            response = await call_next(request)
            response.headers["x-hive-forwarded"] = "1"
            return response
        return await call_next(request)

    @app.get("/broker/minds")
    async def minds():
        # Each mind has an address of its own (a different loopback address on
        # the one port), as it has on a real hive: the console finds which mind
        # is meant by the address, and two minds at one address name nobody.
        port = base["url"].rsplit(":", 1)[-1]
        return [dict(m, gateway_url=f"http://127.0.0.{n + 2}:{port}") for n, m in enumerate(MINDS)]

    # The inference proxy's meter, per holder, as it answers the console.
    usage: list[dict] = [
        {"holder": "skippy", "role": "mind", "requests": 40, "total_tokens": 9000, "cost": 1.5},
        {"holder": "arnold", "role": "mind", "requests": 3, "total_tokens": 700, "cost": 0.25},
        {"holder": "hive-tools", "role": "service", "requests": 2, "total_tokens": 50, "cost": 0.01},
    ]

    @app.get("/credentials/usage")
    async def credentials_usage(days: int = 30):
        return usage

    @app.get("/sessions")
    async def sessions():
        return SESSIONS

    @app.get("/sessions/{session_id}")
    async def session(session_id: str):
        for row in SESSIONS:
            if row["id"] == session_id:
                return row
        return JSONResponse({"error": "Session not found"}, status_code=404)

    # Bot claims, and every mind's own surface-token route — both reachable
    # here because each mind's gateway_url points back at this app. Held in
    # mutable state so a test can set the hive up and read back what the
    # console actually wrote.
    claims: dict[tuple[str, str], dict] = {}
    absent = {"claim_routes": False, "surface_echo": False}
    tokens: dict[tuple[str, str], dict] = {}
    unreachable: set[tuple[str, str]] = set()

    @app.get("/broker/minds/{mind_id}/surface-claim")
    async def get_claim(mind_id: str, surface: str = "telegram"):
        if absent["claim_routes"]:
            # A gateway that predates bot claims. Distinct from one that
            # cannot be reached at all, which is a different sentence.
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        row = claims.get((mind_id, surface), {})
        return {
            "mind_id": mind_id,
            "surface": surface,
            "claimed": bool(row.get("claimed_ref")),
            "claimed_ref": row.get("claimed_ref", ""),
            "phrase_pending": bool(row.get("phrase")),
            "phrase_expires_at": row.get("expires_at", 0),
        }

    @app.post("/broker/minds/{mind_id}/surface-claim/phrase")
    async def mint_claim(mind_id: str, surface: str = "telegram"):
        row = claims.setdefault((mind_id, surface), {})
        if row.get("claimed_ref"):
            return JSONResponse({"error": "already claimed"}, status_code=409)
        row["phrase"] = f"anchor basalt {len(claims)}"
        row["expires_at"] = 1800
        return {"phrase": row["phrase"], "surface": surface, "expires_at": 1800}

    @app.delete("/broker/minds/{mind_id}/surface-claim")
    async def clear_claim(mind_id: str, surface: str = "telegram"):
        claims[(mind_id, surface)] = {}
        return {"ok": True, "mind_id": mind_id, "surface": surface}

    @app.get("/surface-token")
    async def surface_token(request: Request, surface: str = "telegram"):
        mind = request.query_params.get("mind", "")
        if (mind, surface) in unreachable:
            return JSONResponse({"error": "down"}, status_code=502)
        if absent["surface_echo"]:
            # A mind that predates the surface parameter: it drops it without
            # comment and answers about Telegram.
            return {"stored": True, "accepted": True,
                    "bot_username": "telegram-bot", "where": "env"}
        row = tokens.get((mind, surface))
        if row is None:
            return {"stored": False, "accepted": None, "surface": surface,
                    "where": "env"}
        return dict(row, surface=surface, where="env")

    @app.put("/surface-token")
    async def set_surface_token(request: Request, surface: str = "telegram"):
        body = await request.json()
        token = str((body or {}).get("token") or "")
        if not token.startswith("good"):
            return JSONResponse({"error": "bot API refused this token"}, status_code=400)
        return {
            "saved": True, "stored": True, "accepted": True,
            "bot_username": f"{surface}-bot", "surface": surface, "where": "env",
        }

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
    app.state.claims = claims
    app.state.absent = absent
    app.state.tokens = tokens
    app.state.unreachable = unreachable
    app.state.usage = usage
    return app


class FakeComms(ThreadedServer):
    def __init__(self):
        app = fake_comms_app()
        super().__init__(app)
        app.state.base["url"] = self.url
        # Every write it was sent: what the console asked of comms, as it went.
        self.received = app.state.received
        self.claims = app.state.claims
        self.absent = app.state.absent
        self.tokens = app.state.tokens
        self.unreachable = app.state.unreachable


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

    def _admin_token(self) -> str:
        """The seeded administrator's own sign-in; no shared service key exists."""
        reply = httpx.post(
            f"{self.url}/login",
            json={"username": SEED_USERNAME, "password": SEED_PASSWORD},
            timeout=10,
        )
        reply.raise_for_status()
        return reply.json()["token"]

    def admin(self, method: str, path: str, **kwargs) -> httpx.Response:
        return httpx.request(
            method,
            f"{self.url}{path}",
            headers={"Authorization": f"Bearer {self._admin_token()}"},
            timeout=10,
            **kwargs,
        )
