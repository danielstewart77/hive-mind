"""
Remote Admin Service — SSH bridge for remote Hive Mind installation and management.
Exposes paramiko SSH sessions over HTTP + WebSocket on port 8430.

Auth: Authorization: Bearer <token>  (REMOTE_ADMIN_TOKEN env var)
WebSocket auth: ?token=<token> query param
"""

import asyncio
import io
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import paramiko
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

app = FastAPI(title="Remote Admin", version="1.0.0")
security = HTTPBearer()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

ADMIN_TOKEN = os.getenv("REMOTE_ADMIN_TOKEN", "")


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="REMOTE_ADMIN_TOKEN not configured")
    if credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return credentials.credentials


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ConnectRequest(BaseModel):
    host: str
    port: int = 22
    username: str
    password: Optional[str] = None
    private_key: Optional[str] = None  # PEM text
    timeout: int = 10


class ExecRequest(BaseModel):
    command: str
    timeout: int = 30


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


@dataclass
class SSHSession:
    id: str
    host: str
    port: int
    username: str
    client: paramiko.SSHClient
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def info(self) -> dict:
        return {
            "id": self.id,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "created_at": self.created_at,
        }


_sessions: dict[str, SSHSession] = {}


def _get(session_id: str) -> SSHSession:
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    return _sessions[session_id]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/sessions", status_code=201)
def create_session(req: ConnectRequest, _: str = Depends(verify_token)):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = dict(
        hostname=req.host,
        port=req.port,
        username=req.username,
        timeout=req.timeout,
        look_for_keys=False,
        allow_agent=False,
    )

    if req.private_key:
        pkey = paramiko.RSAKey.from_private_key(io.StringIO(req.private_key))
        connect_kwargs["pkey"] = pkey
    elif req.password:
        connect_kwargs["password"] = req.password
    else:
        raise HTTPException(status_code=400, detail="Provide password or private_key")

    try:
        client.connect(**connect_kwargs)
    except paramiko.AuthenticationException:
        raise HTTPException(status_code=401, detail="SSH authentication failed")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SSH connection failed: {exc}")

    session_id = str(uuid.uuid4())[:8]
    _sessions[session_id] = SSHSession(
        id=session_id,
        host=req.host,
        port=req.port,
        username=req.username,
        client=client,
    )
    return {"session_id": session_id, **_sessions[session_id].info()}


@app.get("/sessions")
def list_sessions(_: str = Depends(verify_token)):
    return [s.info() for s in _sessions.values()]


@app.get("/sessions/{session_id}")
def get_session_detail(session_id: str, _: str = Depends(verify_token)):
    return _get(session_id).info()


@app.delete("/sessions/{session_id}")
def close_session(session_id: str, _: str = Depends(verify_token)):
    session = _get(session_id)
    session.client.close()
    del _sessions[session_id]
    return {"closed": session_id}


@app.post("/sessions/{session_id}/exec")
def exec_command(session_id: str, req: ExecRequest, _: str = Depends(verify_token)):
    session = _get(session_id)
    try:
        _stdin, stdout, stderr = session.client.exec_command(req.command, timeout=req.timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        exit_code = stdout.channel.recv_exit_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Command execution failed: {exc}")
    return {"stdout": out, "stderr": err, "exit_code": exit_code}


@app.websocket("/sessions/{session_id}/stream")
async def stream_session(websocket: WebSocket, session_id: str):
    """Interactive WebSocket shell. Auth via ?token= query param."""
    token = websocket.query_params.get("token", "")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    if session_id not in _sessions:
        await websocket.close(code=1008, reason="Session not found")
        return

    session = _sessions[session_id]
    await websocket.accept()

    channel = session.client.invoke_shell(term="xterm", width=220, height=50)
    channel.setblocking(False)

    async def read_ssh() -> None:
        while True:
            await asyncio.sleep(0.05)
            try:
                if channel.recv_ready():
                    data = channel.recv(4096).decode(errors="replace")
                    await websocket.send_text(data)
                if channel.closed or channel.exit_status_ready():
                    break
            except Exception:
                break

    async def write_ssh() -> None:
        while True:
            try:
                data = await websocket.receive_text()
                channel.sendall(data.encode())
            except WebSocketDisconnect:
                break
            except Exception:
                break

    try:
        read_task = asyncio.create_task(read_ssh())
        write_task = asyncio.create_task(write_ssh())
        _done, pending = await asyncio.wait(
            [read_task, write_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        channel.close()
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8430)
