"""Tests for WS /sessions/{session_id}/attach — the browser-terminal proxy.

Covers: bearer gating, 404s for an unknown session or unregistered mind,
and the byte-pump bridge to a fake mind-side attach-pty WS in both
directions.
"""
from __future__ import annotations

import asyncio
import os
import time

import aiohttp
import pytest
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


class _FakeMindWS:
    """Stands in for aiohttp's ClientWebSocketResponse.

    Closing is modelled the way aiohttp actually behaves: a CLOSE frame is
    never handed to `async for` — iteration simply ends and the code is
    left on `.close_code`. A fake that yields a CLOSE message instead lets
    a proxy that reads the code out of the message pass its tests and lose
    every close code in production.
    """

    def __init__(self, incoming: list[bytes] | None = None, close_with: int | None = None):
        self._incoming = list(incoming or [])
        self._close_with = close_with
        self._block = asyncio.Event()
        self.sent: list[bytes] = []
        self.close_code: int | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._incoming:
            data = self._incoming.pop(0)
            return type("Msg", (), {"type": aiohttp.WSMsgType.BINARY, "data": data})()
        if self._close_with is not None:
            self.close_code = self._close_with
            self._close_with = None
            raise StopAsyncIteration
        await self._block.wait()  # never set — blocks until the pump is cancelled
        raise StopAsyncIteration

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeHttpSession:
    ws_to_return: _FakeMindWS | None = None
    requested_url: str | None = None
    raise_on_connect: Exception | None = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def ws_connect(self, url, **kwargs):
        type(self).requested_url = url
        if type(self).raise_on_connect:
            raise type(self).raise_on_connect
        return type(self).ws_to_return


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)

    import importlib

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        yield client, server_module


async def _seed_session_and_mind(server_module, session_id="sess-attach", mind_id="ada", claude_sid="claude-abc"):
    from comms import broker

    mgr = server_module.session_mgr
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at,
                                  last_active, status, mind_id, summary)
           VALUES (?, ?, 'web', 'u1', 'opus', ?, ?, 'running', ?, 'terminal attach')""",
        (session_id, claude_sid, now, now, mind_id),
    )
    await mgr._db.commit()
    await broker.register_mind(
        mgr.broker_db, mind_id=mind_id, name=mind_id, gateway_url="http://mind.test:8420",
        model="opus", harness="claude",
    )


class TestWsAttach:
    def test_unknown_session_closes_4404(self, app_client):
        client, server_module = app_client
        with pytest.raises(Exception):
            with client.websocket_connect("/sessions/nope/attach") as ws:
                ws.receive_bytes()

    def test_unregistered_mind_closes_4404(self, app_client):
        client, server_module = app_client
        mgr = server_module.session_mgr
        now = time.time()
        _run(mgr._db.execute(
            """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at,
                                      last_active, status, mind_id, summary)
               VALUES ('sess-orphan', 'x', 'web', 'u1', 'opus', ?, ?, 'running', 'ghost', 's')""",
            (now, now),
        ))
        _run(mgr._db.commit())

        with pytest.raises(Exception):
            with client.websocket_connect("/sessions/sess-orphan/attach") as ws:
                ws.receive_bytes()

    def test_bearer_gate_rejects_bad_token(self, app_client, monkeypatch):
        client, server_module = app_client
        monkeypatch.setenv("COMMS_BEARER_TOKEN", "secret-token")

        with pytest.raises(Exception):
            with client.websocket_connect(
                "/sessions/sess-attach/attach",
                headers={"authorization": "Bearer wrong"},
            ) as ws:
                ws.receive_bytes()

    def test_relays_mind_output_to_browser(self, app_client, monkeypatch):
        client, server_module = app_client
        _run(_seed_session_and_mind(server_module))

        fake_ws = _FakeMindWS(incoming=[b"hello from the tui\r\n"])
        _FakeHttpSession.ws_to_return = fake_ws
        _FakeHttpSession.raise_on_connect = None
        monkeypatch.setattr(server_module.aiohttp, "ClientSession", _FakeHttpSession)

        with client.websocket_connect("/sessions/sess-attach/attach") as ws:
            assert ws.receive_bytes() == b"hello from the tui\r\n"

        assert "ws://mind.test:8420/sessions/sess-attach/attach-pty" in _FakeHttpSession.requested_url
        assert "resume_sid=claude-abc" in _FakeHttpSession.requested_url
        assert "model=opus" in _FakeHttpSession.requested_url

    def test_relays_browser_input_to_mind(self, app_client, monkeypatch):
        client, server_module = app_client
        _run(_seed_session_and_mind(server_module, session_id="sess-attach2"))

        fake_ws = _FakeMindWS(incoming=[])
        _FakeHttpSession.ws_to_return = fake_ws
        _FakeHttpSession.raise_on_connect = None
        monkeypatch.setattr(server_module.aiohttp, "ClientSession", _FakeHttpSession)

        with client.websocket_connect("/sessions/sess-attach2/attach") as ws:
            ws.send_bytes(b"/help\n")
            for _ in range(20):
                if fake_ws.sent:
                    break
                time.sleep(0.05)
            assert fake_ws.sent == [b"/help\n"]

    def test_mind_unreachable_closes_1011(self, app_client, monkeypatch):
        client, server_module = app_client
        _run(_seed_session_and_mind(server_module, session_id="sess-attach3"))

        _FakeHttpSession.ws_to_return = None
        _FakeHttpSession.raise_on_connect = aiohttp.ClientConnectionError("refused")
        monkeypatch.setattr(server_module.aiohttp, "ClientSession", _FakeHttpSession)

        with pytest.raises(Exception):
            with client.websocket_connect("/sessions/sess-attach3/attach") as ws:
                ws.receive_bytes()

    def test_mind_without_pty_route_closes_4415(self, app_client, monkeypatch):
        """A mind whose image predates the pty work answers the handshake
        with 403. That is permanent, not a blip, so it must not share the
        1011 code a client retries on."""
        from starlette.websockets import WebSocketDisconnect

        client, server_module = app_client
        _run(_seed_session_and_mind(server_module, session_id="sess-nopty"))

        _FakeHttpSession.ws_to_return = None
        _FakeHttpSession.raise_on_connect = aiohttp.WSServerHandshakeError(
            aiohttp.RequestInfo(
                url="ws://mind.test:8420/attach-pty", method="GET",
                headers=aiohttp.typedefs.CIMultiDict(), real_url="ws://mind.test:8420/attach-pty",
            ),
            (),
            status=403,
            message="Invalid response status",
        )
        monkeypatch.setattr(server_module.aiohttp, "ClientSession", _FakeHttpSession)

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/sessions/sess-nopty/attach") as ws:
                ws.receive_bytes()

        assert excinfo.value.code == 4415

    def test_kill_session_tears_down_live_attach_with_4410(self, app_client, monkeypatch):
        """The attach pty is a separate process kill_session can't reach;
        the bridge must notice the session_closed event and drop, closing
        the browser side with 4410 so the tile reports the ending instead
        of hunting a successor."""
        from starlette.websockets import WebSocketDisconnect

        client, server_module = app_client
        _run(_seed_session_and_mind(server_module, session_id="sess-end"))

        fake_ws = _FakeMindWS(incoming=[b"tui up\r\n"])
        _FakeHttpSession.ws_to_return = fake_ws
        _FakeHttpSession.raise_on_connect = None
        monkeypatch.setattr(server_module.aiohttp, "ClientSession", _FakeHttpSession)

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/sessions/sess-end/attach") as ws:
                assert ws.receive_bytes() == b"tui up\r\n"  # bridge is live
                client.portal.call(server_module.session_mgr.kill_session, "sess-end")
                ws.receive_bytes()  # blocks until the watcher closes the bridge

        assert excinfo.value.code == 4410

    def test_mind_close_code_reaches_the_browser(self, app_client, monkeypatch):
        """A mind evicting a stale attach closes 1012, which is the tile's
        only signal to stand down rather than reconnect. Swallowing it into
        a plain close makes two tiles evict each other in a loop."""
        from starlette.websockets import WebSocketDisconnect

        client, server_module = app_client
        _run(_seed_session_and_mind(server_module, session_id="sess-evicted"))

        fake_ws = _FakeMindWS(incoming=[b"tui up\r\n"], close_with=1012)
        _FakeHttpSession.ws_to_return = fake_ws
        _FakeHttpSession.raise_on_connect = None
        monkeypatch.setattr(server_module.aiohttp, "ClientSession", _FakeHttpSession)

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/sessions/sess-evicted/attach") as ws:
                assert ws.receive_bytes() == b"tui up\r\n"
                ws.receive_bytes()  # blocks until the eviction propagates

        assert excinfo.value.code == 1012

    def test_terminal_exit_code_reaches_the_browser(self, app_client, monkeypatch):
        """Same path for a pty that exited: 1000 means the terminal is gone,
        not that the socket dropped."""
        from starlette.websockets import WebSocketDisconnect

        client, server_module = app_client
        _run(_seed_session_and_mind(server_module, session_id="sess-exited"))

        fake_ws = _FakeMindWS(incoming=[b"bye\r\n"], close_with=1000)
        _FakeHttpSession.ws_to_return = fake_ws
        _FakeHttpSession.raise_on_connect = None
        monkeypatch.setattr(server_module.aiohttp, "ClientSession", _FakeHttpSession)

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/sessions/sess-exited/attach") as ws:
                assert ws.receive_bytes() == b"bye\r\n"
                ws.receive_bytes()

        assert excinfo.value.code == 1000

    def test_attach_to_archived_session_is_not_immediately_closed(self, app_client, monkeypatch):
        """Opening an already-closed (archived) session is a deliberate
        resurrection: the close-watcher must not arm, or it would fire the
        instant the bridge opens."""
        client, server_module = app_client
        _run(_seed_session_and_mind(server_module, session_id="sess-archived"))
        mgr = server_module.session_mgr
        _run(mgr._db.execute(
            "UPDATE sessions SET status = 'closed' WHERE id = 'sess-archived'"
        ))
        _run(mgr._db.commit())

        fake_ws = _FakeMindWS(incoming=[b"resurrected\r\n"])
        _FakeHttpSession.ws_to_return = fake_ws
        _FakeHttpSession.raise_on_connect = None
        monkeypatch.setattr(server_module.aiohttp, "ClientSession", _FakeHttpSession)

        with client.websocket_connect("/sessions/sess-archived/attach") as ws:
            assert ws.receive_bytes() == b"resurrected\r\n"
            ws.send_bytes(b"hi\n")
            for _ in range(20):
                if fake_ws.sent:
                    break
                time.sleep(0.05)
            assert fake_ws.sent == [b"hi\n"]


class TestAttachRequiresAConversation:
    """A tile that appears attachable but has no conversation must fail
    visibly rather than opening as a blank terminal."""

    def test_session_without_a_conversation_id_is_refused(self, app_client):
        client, server_module = app_client
        _run(_seed_session_and_mind(
            server_module, session_id="sess-hollow", claude_sid=None
        ))

        with pytest.raises(Exception):
            with client.websocket_connect("/sessions/sess-hollow/attach") as ws:
                ws.receive_bytes()
