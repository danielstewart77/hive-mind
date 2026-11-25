"""P1/P2: the real gateway enforces owner checks and closes live sockets."""
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from remote_access.service import make_gateway
from remote_access.tests.test_manager import connectors, configure  # noqa: F401


def test_gateway_refuses_wrong_owner_even_on_terminal_and_strips_forged_identity(connectors):
    manager, _ = connectors
    configure(manager, "alice", 1)
    manager.arm("alice")
    def upstream(request):
        if request.url.path == "/api/remote-access/authorize":
            body = json.loads(request.content)
            return httpx.Response(200 if request.headers.get("cookie") == "user=" + body["owner"] else 403)
        if request.url.path == "/login":
            return httpx.Response(200, stream=httpx.ByteStream(('src="http://' + request.headers["host"] + '/static/login.js"').encode()), headers={"content-type": "text/html"})
        # Echo the actual forwarded identity, never one chosen by the client.
        return httpx.Response(200, stream=httpx.ByteStream(request.headers.get("x-hive-remote-owner", "").encode()), headers={"set-cookie": "session=signed; Domain=lan.example; Secure; HttpOnly"})
    with TestClient(make_gateway(manager, transport=httpx.MockTransport(upstream))) as client:
        for host in ("hive.alice.example.com", "terminal.alice.example.com"):
            assert 'src="https://' + host + '/static/login.js"' in client.get("/login", headers={"host": host}).text
            assert client.get("/api/private", headers={"host": host, "cookie": "user=admin"}).status_code == 403
            response = client.get("/api/private", headers={"host": host, "cookie": "user=alice", "x-hive-remote-owner": "admin", "x-hive-remote-key": "forged"})
            assert response.status_code == 200
            assert response.text == "alice"
            assert "Domain=" not in response.headers["set-cookie"]
        manager.set_allowed(False)
        assert client.get("/login", headers={"host": "hive.alice.example.com"}).status_code == 403


def test_disarming_closes_an_open_terminal_websocket(connectors):
    manager, _ = connectors
    configure(manager, "alice", 1)
    manager.arm("alice")
    origin = FastAPI()
    @origin.websocket("/ws/terminal")
    async def terminal(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_text(await ws.receive_text())
        except WebSocketDisconnect:
            pass
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(origin, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    until = time.monotonic() + 5
    while not server.started and time.monotonic() < until:
        time.sleep(.02)
    manager.terminal_origin = f"http://127.0.0.1:{port}"
    transport = httpx.MockTransport(lambda request: httpx.Response(200 if request.headers.get("cookie") == "user=alice" else 403))
    try:
        with TestClient(make_gateway(manager, transport=transport)) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws/terminal", headers={"host": "terminal.alice.example.com", "cookie": "user=bob"}):
                    pass
            with client.websocket_connect("/ws/terminal", headers={"host": "terminal.alice.example.com", "cookie": "user=alice"}) as ws:
                ws.send_text("conversation remains upstream")
                assert ws.receive_text() == "conversation remains upstream"
                manager.off("alice")
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_text()
                assert closed.value.code == 4403
    finally:
        server.should_exit = True
        thread.join(5)
