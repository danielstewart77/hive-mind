"""Route tests for login, the /console SSE proxy, and the /terminal pty attach."""

import asyncio
import json
import os
import sys
from unittest.mock import patch

import httpx
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


class _FakeStreamResponse:
    def __init__(self, lines, raise_for_status_error=None):
        self._lines = lines
        self._raise = raise_for_status_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamError:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *args):
        return False


class _FakeAsyncClient:
    def __init__(self, stream_cm):
        self._stream_cm = stream_cm

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, headers=None):
        return self._stream_cm


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_login_sets_cookie_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)

    response = client.post(
        "/login",
        data={"username": "daniel", "password": "secret-pass", "next": "/terminal"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/terminal"
    assert auth.SESSION_COOKIE_NAME in response.cookies


# --- /console SSE proxy (untouched by the terminal rewrite) ----------------


def test_console_stream_proxies_sse(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    async def fake_proxy(session_id):
        yield "data: {\"type\":\"assistant\",\"content\":\"hello\"}\n\n"

    with patch("main._proxy_session_events", side_effect=fake_proxy):
        response = client.get("/api/console/sess-1/stream")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "assistant" in response.text


def test_proxy_session_events_does_not_fabricate_session_closed_on_timeout():
    stream_cm = _FakeStreamError(httpx.ConnectError("timed out"))
    fake_client = _FakeAsyncClient(stream_cm)
    with patch("main.httpx.AsyncClient", return_value=fake_client):
        events = asyncio.run(_collect(main_mod._proxy_session_events("sess-1")))

    assert len(events) == 1
    payload = json.loads(events[0][len("data: "):].rstrip("\n"))
    assert payload["type"] == "system"
    assert "upstream_error" in payload["content"]
    assert "session_closed" not in events[0]


def test_proxy_session_events_preserves_real_session_closed():
    lines = [
        'data: {"type":"assistant","content":"hello"}',
        "",
        'data: {"type":"session_closed","session_id":"sess-1"}',
        "",
    ]
    stream_cm = _FakeStreamResponse(lines)
    fake_client = _FakeAsyncClient(stream_cm)
    with patch("main.httpx.AsyncClient", return_value=fake_client):
        events = asyncio.run(_collect(main_mod._proxy_session_events("sess-1")))

    assert events == [
        'data: {"type":"assistant","content":"hello"}\n\n',
        'data: {"type":"session_closed","session_id":"sess-1"}\n\n',
    ]


def test_console_stream_sets_anti_buffering_headers(tmp_path, monkeypatch):
    """SSE must carry no-cache/no-transform + X-Accel-Buffering headers so
    intermediaries (Cloudflare) neither buffer nor transform the stream —
    buffered SSE arrives in delayed bursts and loses events on idle cuts."""
    client = _authed_client(tmp_path, monkeypatch)

    async def fake_proxy(session_id):
        yield "data: {\"type\":\"ping\"}\n\n"

    with patch("main._proxy_session_events", side_effect=fake_proxy):
        response = client.get("/api/console/sess-1/stream")

    assert response.status_code == 200
    assert "no-cache" in response.headers["cache-control"]
    assert "no-transform" in response.headers["cache-control"]
    assert response.headers["x-accel-buffering"] == "no"


def test_proxy_session_events_passes_ping_through():
    """Gateway heartbeat pings must reach the browser to keep the SSE
    connection warm through proxy idle timeouts."""
    lines = [
        'data: {"type":"ping","session_id":"sess-1"}',
        "",
        'data: {"type":"assistant","content":"hi"}',
        "",
    ]
    stream_cm = _FakeStreamResponse(lines)
    fake_client = _FakeAsyncClient(stream_cm)
    with patch("main.httpx.AsyncClient", return_value=fake_client):
        events = asyncio.run(_collect(main_mod._proxy_session_events("sess-1")))

    assert events[0] == 'data: {"type":"ping","session_id":"sess-1"}\n\n'


# --- /api/terminal/tts (untouched by the terminal rewrite) ------------------


def test_api_terminal_tts_proxies_to_voice_server(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("VOICE_API_URL", "http://hive-mind-voice:8422")

    captured = {}

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"OGGDATA"
        def getheader(self, name, default=None):
            return "audio/ogg" if name.lower() == "content-type" else default
        headers = {"content-type": "audio/ogg"}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return _FakeResp()

    with patch("main.urllib.request.urlopen", side_effect=_fake_urlopen):
        response = client.post(
            "/api/terminal/tts",
            json={"text": "hello world", "voice_id": "abc-123"},
        )

    assert response.status_code == 200
    assert response.content == b"OGGDATA"
    assert "/tts" in captured["url"]
    sent = json.loads(captured["body"].decode())
    assert sent["text"] == "hello world"
    assert sent["voice_id"] == "abc-123"


def test_api_terminal_tts_requires_text(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.post("/api/terminal/tts", json={"voice_id": "x"})
    assert response.status_code == 400


# --- /terminal page (rewritten: xterm.js canvas, not chat panels) -----------


def test_terminal_page_renders_xterm_shell(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.get("/terminal")

    assert response.status_code == 200
    body = response.text
    assert "vendor/xterm/xterm.js" in body
    assert "vendor/xterm/addon-fit.js" in body
    assert "/api/terminal/attach/" in body
    assert "/api/terminal/sessions" in body


def test_terminal_page_renders_session_manager_shell(tmp_path, monkeypatch):
    """The xterm tiles live inside the session-manager shell: agents rail,
    Brady-Bunch grid, rename/recolor editor, TTS speaker, mobile toolbar."""
    client = _authed_client(tmp_path, monkeypatch)
    response = client.get("/terminal")

    assert response.status_code == 200
    body = response.text
    # agents rail with filter tabs and collapse/expand
    assert 'id="term-list-view"' in body
    assert 'id="term-list"' in body
    assert 'id="term-tab-active"' in body
    assert 'id="term-tab-suspended"' in body
    assert "term-panel-suspend" in body
    assert 'id="term-rail-collapse"' in body
    assert 'id="term-rail-expand"' in body
    # tile grid stage
    assert 'id="term-grid"' in body
    assert 'id="term-grid-empty"' in body
    # per-session rename/recolor editor (JS-built panel markup); the color
    # grid is inline in the editor, always open — no pop-out button
    assert "term-rename-pop" in body
    assert "term-color-grid-swatches" in body
    assert "term-swatch-more" not in body
    assert "term-labels" in body
    # TTS speaker wired to the console SSE stream + tts proxy
    assert "term-panel-speaker" in body
    assert "/api/terminal/tts" in body
    assert "/api/console/" in body
    # mobile keys toolbar
    assert 'id="term-mobile-toolbar"' in body


def test_terminal_css_keeps_rail_grid_and_speaker_styles():
    """The reintegrated shell needs its styles: collapsible rail, grid tiles,
    swatch editor, speaker-on state, and mobile view switching."""
    css_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "static", "style.css"
    )
    with open(css_path, encoding="utf-8") as fh:
        css = fh.read()
    assert '.term-app[data-rail="collapsed"]' in css
    assert ".term-grid" in css
    assert ".term-swatch" in css
    assert ".term-speaker-on" in css
    assert '.term-app[data-view="list"]' in css
    assert ".term-card-dot" in css


def test_terminal_page_redirects_when_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.get("/terminal", follow_redirects=False)

    assert response.status_code in (302, 303)


def test_terminal_css_hides_intro_banner_not_nav():
    """The terminal page hides the personal intro banner but keeps the nav bar."""
    css_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "static", "style.css"
    )
    with open(css_path, encoding="utf-8") as fh:
        css = fh.read()
    assert "body:has(.terminal-page) .content-wrapper > .terminal-box" in css
    assert "body:has(.terminal-page) nav { display: none" not in css
    assert "body:has(.terminal-page) nav{display:none" not in css


# --- GET /api/terminal/sessions ---------------------------------------------


def test_api_terminal_sessions_returns_flat_labeled_list(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    now = int(__import__("time").time())

    async def fake_gateway_json(path, *a, **kw):
        if path == "/broker/minds":
            return [{"id": "ada-id", "name": "ada"}, {"id": "skippy-id", "name": "skippy"}]
        if path == "/sessions":
            return [
                {"id": "sess-old12345", "mind_id": "ada-id", "status": "idle",
                 "last_active": now - 120, "summary": "older"},
                {"id": "sess-new12345", "mind_id": "skippy-id", "status": "running",
                 "last_active": now - 5, "summary": "newest"},
                {"id": "sess-sched", "mind_id": "ada-id", "status": "running",
                 "owner_type": "scheduler", "last_active": now - 1, "summary": "cron"},
                {"id": "sess-suspended", "mind_id": "ada-id", "status": "suspended",
                 "last_active": now - 30, "summary": "parked"},
                {"id": "sess-closed", "mind_id": "ada-id", "status": "closed",
                 "last_active": now - 10, "summary": "history"},
            ]
        return []

    with patch("main._gateway_json", side_effect=fake_gateway_json):
        response = client.get("/api/terminal/sessions")

    assert response.status_code == 200
    rows = response.json()
    ids = [r["id"] for r in rows]
    assert "sess-sched" not in ids  # scheduler sessions excluded
    assert "sess-closed" not in ids  # historical sessions stay out of the terminal
    assert ids == ["sess-new12345", "sess-suspended", "sess-old12345"]
    newest = rows[0]
    assert newest["mind_name"] == "skippy"
    assert newest["short_id"] == "sess-new"
    assert newest["status"] == "running"
    assert newest["age"].endswith("ago")
    assert newest["summary"] == "newest"


def test_api_terminal_sessions_empty_on_gateway_error(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    async def fake_gateway_json(path, *a, **kw):
        raise RuntimeError("gateway unreachable")

    with patch("main._gateway_json", side_effect=fake_gateway_json):
        response = client.get("/api/terminal/sessions")

    assert response.status_code == 200
    assert response.json() == []


# --- POST /api/terminal/sessions --------------------------------------------


class _FakeUrlopen:
    """Captures posted bodies for _create_gateway_session."""

    def __init__(self, captured):
        self.captured = captured

    def __call__(self, req, timeout=None):
        self.captured.append(json.loads(req.data.decode("utf-8")))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"id": "new-session"}).encode("utf-8")


def test_terminal_session_create_uses_unique_client_ref_per_call(tmp_path, monkeypatch):
    """Each POST must get its own active_sessions binding.

    A shared constant client_ref makes calls collide on the gateway's
    (client_type, client_ref) primary key, so rotation arms the wrong session
    and carry-forward memory lands in the wrong place.
    """
    client = _authed_client(tmp_path, monkeypatch)
    captured = []

    with patch("main.urllib.request.urlopen", _FakeUrlopen(captured)):
        first = client.post("/api/terminal/sessions", json={"mind_id": "skippy-id"})
        second = client.post("/api/terminal/sessions", json={"mind_id": "skippy-id"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(captured) == 2

    refs = [body["client_ref"] for body in captured]
    assert refs[0] != refs[1], "two calls shared one client_ref"
    assert all(ref.startswith("terminal-") for ref in refs)
    assert all(body["owner_ref"] == "terminal" for body in captured)


def test_terminal_session_create_requires_mind_id(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.post("/api/terminal/sessions", json={})
    assert response.status_code == 400


@pytest.mark.parametrize("action", ["suspend", "resume"])
def test_terminal_session_lifecycle_actions_proxy_to_gateway(tmp_path, monkeypatch, action):
    client = _authed_client(tmp_path, monkeypatch)
    captured = {}

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            status = "suspended" if action == "suspend" else "idle"
            return json.dumps({"id": "sess-1", "status": status}).encode()

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data) if req.data else None
        return _FakeResp()

    with patch("main.urllib.request.urlopen", side_effect=_fake_urlopen):
        response = client.post(f"/api/terminal/sessions/sess-1/{action}")

    assert response.status_code == 200
    assert response.json()["status"] == ("suspended" if action == "suspend" else "idle")
    assert captured["method"] == "POST"
    assert f"/sessions/sess-1/{action}" in captured["url"]
    if action == "resume":
        assert captured["body"]["client_type"] == "web"
        assert captured["body"]["client_ref"].startswith("terminal-")
    else:
        assert captured["body"] is None


# --- WS /api/terminal/attach/{session_id} -----------------------------------


class _FakeMindWS:
    """Stands in for a websockets.ClientConnection."""

    def __init__(self, incoming=None, close_code=None, close_reason=None):
        self._incoming = list(incoming or [])
        self._block = asyncio.Event()
        self._ends = close_code is not None
        self.close_code = close_code
        self.close_reason = close_reason
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._incoming:
            return self._incoming.pop(0)
        if self._ends:  # gateway closed the connection
            raise StopAsyncIteration
        await self._block.wait()  # never set — blocks until the pump is cancelled
        raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_attach_ws_rejects_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    with pytest.raises(Exception):
        with client.websocket_connect("/api/terminal/attach/sess-1") as ws:
            ws.receive_bytes()


def test_attach_ws_relays_mind_output_to_browser(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    fake_ws = _FakeMindWS(incoming=[b"hello from the tui\r\n"])

    def _fake_connect(url, **kwargs):
        _fake_connect.requested_url = url
        _fake_connect.requested_headers = kwargs.get("additional_headers")
        return fake_ws

    with patch("main.websockets.connect", _fake_connect):
        with client.websocket_connect("/api/terminal/attach/sess-1") as ws:
            assert ws.receive_bytes() == b"hello from the tui\r\n"

    assert "/sessions/sess-1/attach" in _fake_connect.requested_url


def test_attach_ws_relays_browser_input_to_mind(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    fake_ws = _FakeMindWS(incoming=[])

    def _fake_connect(url, **kwargs):
        return fake_ws

    with patch("main.websockets.connect", _fake_connect):
        with client.websocket_connect("/api/terminal/attach/sess-2") as ws:
            ws.send_bytes(b"/help\n")
            import time
            for _ in range(20):
                if fake_ws.sent:
                    break
                time.sleep(0.05)
            assert fake_ws.sent == [b"/help\n"]


def test_attach_ws_forwards_resize_text_frames_as_text(tmp_path, monkeypatch):
    """TEXT frames are the resize control channel; re-encoding them into the
    byte stream would type JSON into the TUI instead of resizing the pty."""
    client = _authed_client(tmp_path, monkeypatch)
    fake_ws = _FakeMindWS(incoming=[])

    def _fake_connect(url, **kwargs):
        return fake_ws

    resize = '{"type":"resize","cols":100,"rows":30}'
    with patch("main.websockets.connect", _fake_connect):
        with client.websocket_connect("/api/terminal/attach/sess-2") as ws:
            ws.send_text(resize)
            ws.send_bytes(b"ls\n")
            import time
            for _ in range(40):
                if len(fake_ws.sent) >= 2:
                    break
                time.sleep(0.05)
            assert fake_ws.sent == [resize, b"ls\n"]
            assert isinstance(fake_ws.sent[0], str)


def test_attach_ws_passes_tile_geometry_to_gateway(tmp_path, monkeypatch):
    """cols/rows from the browser tile ride the attach URL so the pty spawns
    at the tile's real geometry instead of a blind 80x24."""
    client = _authed_client(tmp_path, monkeypatch)
    fake_ws = _FakeMindWS(incoming=[b"ready"])

    def _fake_connect(url, **kwargs):
        _fake_connect.requested_url = url
        return fake_ws

    with patch("main.websockets.connect", _fake_connect):
        with client.websocket_connect("/api/terminal/attach/sess-1?cols=132&rows=43") as ws:
            ws.receive_bytes()

    assert "cols=132" in _fake_connect.requested_url
    assert "rows=43" in _fake_connect.requested_url


def test_attach_ws_propagates_gateway_close_code(tmp_path, monkeypatch):
    """4410 ("session closed") from the gateway must reach the browser —
    it's how the tile tells a deliberate end from a rotation it should
    hunt a successor for."""
    from starlette.websockets import WebSocketDisconnect

    client = _authed_client(tmp_path, monkeypatch)
    fake_ws = _FakeMindWS(incoming=[b"bye\r\n"], close_code=4410, close_reason="session closed")

    def _fake_connect(url, **kwargs):
        return fake_ws

    with patch("main.websockets.connect", _fake_connect):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/api/terminal/attach/sess-1") as ws:
                assert ws.receive_bytes() == b"bye\r\n"
                ws.receive_bytes()  # gateway closes; proxy re-closes with its code

    assert excinfo.value.code == 4410


def test_attach_ws_propagates_eviction_close_code(tmp_path, monkeypatch):
    """1012 ("attached elsewhere") must reach the browser too.

    It is a standard-range code, not a private 4xxx one, and swallowing it
    is what turned an eviction into a tug-of-war: the evicted tile read a
    plain disconnect, reconnected, evicted the tile that had just taken
    over, and both terminals repainted each other's geometry on loop.
    """
    from starlette.websockets import WebSocketDisconnect

    client = _authed_client(tmp_path, monkeypatch)
    fake_ws = _FakeMindWS(incoming=[b"x"], close_code=1012, close_reason="attached elsewhere")

    def _fake_connect(url, **kwargs):
        return fake_ws

    with patch("main.websockets.connect", _fake_connect):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/api/terminal/attach/sess-1") as ws:
                assert ws.receive_bytes() == b"x"
                ws.receive_bytes()

    assert excinfo.value.code == 1012


def test_relayable_close_code_keeps_standard_range_codes():
    """1012 ("attached elsewhere") and 1008 ("refused") are standard-range,
    not private 4xxx — swallowing them is what turned an eviction into a
    tug-of-war between the phone and the desktop tile."""
    assert main_mod._relayable_close_code(1012) == 1012
    assert main_mod._relayable_close_code(1008) == 1008
    assert main_mod._relayable_close_code(4410) == 4410


def test_relayable_close_code_drops_synthetic_markers():
    """1005/1006 mean "no close frame arrived" — local markers that cannot
    legally be sent on the wire."""
    assert main_mod._relayable_close_code(1006) is None
    assert main_mod._relayable_close_code(1005) is None
    assert main_mod._relayable_close_code(None) is None


def test_attach_ws_closes_1011_when_gateway_unreachable(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    def _fake_connect(url, **kwargs):
        raise OSError("connection refused")

    with patch("main.websockets.connect", _fake_connect):
        with pytest.raises(Exception):
            with client.websocket_connect("/api/terminal/attach/sess-3") as ws:
                ws.receive_bytes()
