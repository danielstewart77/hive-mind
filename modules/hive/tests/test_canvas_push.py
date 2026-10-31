"""Tests for the bearer-guarded /canvas/push endpoint that lets Skippy draw."""

import os
import sys

from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main as main_mod
import accounts


def _client(tmp_path, monkeypatch, token="draw-token"):
    monkeypatch.setenv("CANVAS_PUSH_TOKEN", token)
    # Keep canvas state writes off the real repo data dir.
    monkeypatch.setattr(main_mod, "_canvas_state_path", lambda: tmp_path / "canvas_state.json")
    monkeypatch.setattr(main_mod, "_canvas_elements", [])
    return TestClient(main_mod.app)


def test_canvas_push_rejects_missing_bearer(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post("/canvas/push", json={"type": "text", "x": 10, "y": 10, "content": "hi"})

    assert response.status_code == 401
    assert main_mod._canvas_elements == []


def test_canvas_push_rejects_wrong_bearer(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/canvas/push",
        json={"type": "text", "x": 10, "y": 10, "content": "hi"},
        headers={"Authorization": "Bearer nope"},
    )

    assert response.status_code == 401
    assert main_mod._canvas_elements == []


def test_canvas_push_appends_element_with_valid_bearer(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/canvas/push",
        json={"type": "text", "x": 42, "y": 99, "content": "HVAC", "color": "#2dd4bf"},
        headers={"Authorization": "Bearer draw-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["type"] == "text"
    assert body["id"]  # server assigned an id

    assert len(main_mod._canvas_elements) == 1
    stored = main_mod._canvas_elements[0]
    assert stored["content"] == "HVAC"
    assert stored["x"] == 42 and stored["y"] == 99
    assert stored["id"] == body["id"]


def test_canvas_push_rejects_unknown_type(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/canvas/push",
        json={"type": "explode", "x": 1, "y": 1},
        headers={"Authorization": "Bearer draw-token"},
    )

    assert response.status_code == 400
    assert main_mod._canvas_elements == []


def test_canvas_push_falls_back_to_comms_token(tmp_path, monkeypatch):
    monkeypatch.delenv("CANVAS_PUSH_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "house-token")
    monkeypatch.setattr(main_mod, "_canvas_state_path", lambda: tmp_path / "canvas_state.json")
    monkeypatch.setattr(main_mod, "_canvas_elements", [])
    client = TestClient(main_mod.app)

    response = client.post(
        "/canvas/push",
        json={"type": "path", "d": "M0,0 L10,10", "color": "#c9a84c", "sw": 2},
        headers={"Authorization": "Bearer house-token"},
    )

    assert response.status_code == 200
    assert len(main_mod._canvas_elements) == 1
    assert main_mod._canvas_elements[0]["d"] == "M0,0 L10,10"


# ── Render snapshot ──────────────────────────────────────────────────────────


_DRAWER = {"Authorization": "Bearer draw-token"}


def test_canvas_render_svg_refuses_anyone_without_an_admin_session_or_the_bearer(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)

    assert client.get("/canvas/render.svg").status_code == 401
    wrong = client.get("/canvas/render.svg", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401


def test_canvas_render_svg_empty_board(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/canvas/render.svg", headers=_DRAWER)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in response.text
    assert "#090912" in response.text  # dark background rect


def test_canvas_render_svg_includes_elements(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        main_mod,
        "_canvas_elements",
        [
            {"type": "path", "id": "p1", "d": "M10,10 L120,80", "color": "#c9a84c", "sw": 2},
            {"type": "text", "id": "t1", "x": 30, "y": 200, "content": "Capture", "color": "#2dd4bf"},
        ],
    )

    response = client.get("/canvas/render.svg", headers=_DRAWER)

    assert response.status_code == 200
    assert "M10,10 L120,80" in response.text
    assert "Capture" in response.text
    assert "#2dd4bf" in response.text


def test_canvas_render_svg_escapes_text(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        main_mod,
        "_canvas_elements",
        [{"type": "text", "id": "t1", "x": 0, "y": 0, "content": "a < b & c"}],
    )

    response = client.get("/canvas/render.svg", headers=_DRAWER)

    assert "a &lt; b &amp; c" in response.text


# ── Submit poke ──────────────────────────────────────────────────────────────


SKIPPY_ID = "14cb820b-4a42-4f04-a593-54f532fd1d2f"


class _FakeResp:
    status_code = 200

    def __init__(self, payload=None):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    last = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return _FakeResp([{"name": "skippy", "id": SKIPPY_ID}, {"name": "bob", "id": "other"}])

    async def post(self, url, json=None, headers=None):
        _FakeClient.last = {"url": url, "json": json, "headers": headers}
        return _FakeResp()


async def _authed_submit_client(tmp_path, monkeypatch):
    import auth

    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "house-token")
    monkeypatch.setenv("MIND_NAME", "skippy")
    monkeypatch.setattr(main_mod, "_canvas_elements", [{"type": "path", "id": "p1", "d": "M0,0 L5,5"}])
    monkeypatch.setattr(main_mod.httpx, "AsyncClient", _FakeClient)
    _FakeClient.last = None
    user = await auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session_token(user))
    return client


def test_canvas_submit_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.post("/canvas/submit", json={})

    assert response.status_code == 401


async def test_canvas_submit_dispatches_broker_message(tmp_path, monkeypatch):
    client = await _authed_submit_client(tmp_path, monkeypatch)

    response = client.post("/canvas/submit", json={"note": "look at the capture flow"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["dispatched_to"] == "skippy"

    sent = _FakeClient.last
    assert sent is not None
    assert sent["url"].endswith("/broker/messages")
    # Dispatch must use the resolved UUID, not the display name.
    assert sent["json"]["to_mind"] == SKIPPY_ID
    assert sent["json"]["metadata"]["request_type"] == "canvas_review"
    assert "render.svg" in sent["json"]["content"]
    assert "look at the capture flow" in sent["json"]["content"]
    assert sent["headers"]["Authorization"] == "Bearer house-token"
