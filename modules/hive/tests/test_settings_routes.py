"""Route tests for the /settings page and its API endpoints."""

import os
import sys
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_settings_page_redirects_when_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.get("/settings", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/settings")


def test_settings_page_renders_for_authed_user(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.get("/settings")
    assert response.status_code == 200
    assert "mind registry" in response.text


def test_api_settings_minds_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/minds").status_code == 401


def test_api_settings_minds_merges_broker_and_local_config(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    broker_minds = [{"name": "bob", "id": "b9f2", "gateway_url": "http://bob:8420", "model": "qwen3"}]
    with patch.object(main_mod, "_broker_minds_safe", new=AsyncMock(return_value=broker_minds)):
        response = client.get("/api/settings/minds")

    assert response.status_code == 200
    minds = response.json()["minds"]
    assert minds[0]["name"] == "bob"


def test_api_settings_hive_comms_sessions_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.get("/api/settings/hive-comms/sessions").status_code == 401


def test_api_settings_hive_comms_sessions_lists_from_gateway(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    fake_sessions = [{"id": "sess-1", "mind_id": "bob", "status": "idle"}]
    with patch.object(main_mod, "_gateway_json", new=AsyncMock(return_value=fake_sessions)):
        response = client.get("/api/settings/hive-comms/sessions")

    assert response.status_code == 200
    assert response.json()["sessions"] == fake_sessions


def test_api_settings_hive_comms_session_delete_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    assert client.delete("/api/settings/hive-comms/sessions/sess-1").status_code == 401


def test_api_settings_hive_comms_session_delete_proxies_to_gateway(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    class _FakeResp:
        status_code = 200
        content = b'{"ok": true}'
        def json(self):
            return {"ok": True}

    class _FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def delete(self, url, headers=None):
            _FakeAsyncClient.requested_url = url
            return _FakeResp()

    with patch("main.httpx.AsyncClient", _FakeAsyncClient):
        response = client.delete("/api/settings/hive-comms/sessions/sess-1")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "/sessions/sess-1" in _FakeAsyncClient.requested_url
