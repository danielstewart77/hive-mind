"""A new terminal session runs the mind's registered model.

The gateway resolves the model from ``broker.minds`` when the create-session
payload names none. Naming one here pinned every tile of every mind to a
single alias regardless of what that mind is configured to run.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncio

import auth
from hive_site import testing as accounts
import main as main_mod
from starlette.testclient import TestClient


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = accounts.make_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


def _capture_post(monkeypatch):
    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"session_id": "s1"}).encode()

    def _urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(main_mod.urllib.request, "urlopen", _urlopen)
    return sent


def test_create_session_omits_model(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    sent = _capture_post(monkeypatch)

    resp = client.post("/api/terminal/sessions", json={"mind_id": "mind-uuid"})

    assert resp.status_code == 200
    assert "model" not in sent["body"]
    assert sent["body"]["mind_id"] == "mind-uuid"
    assert sent["body"]["owner_ref"] == "terminal"


def test_create_session_requires_mind_id(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    _capture_post(monkeypatch)

    assert client.post("/api/terminal/sessions", json={}).status_code == 400


def test_client_ref_is_unique_per_tile(tmp_path, monkeypatch):
    _authed_client(tmp_path, monkeypatch)
    sent = _capture_post(monkeypatch)

    asyncio.run(main_mod._create_gateway_session("mind-uuid"))
    first = sent["body"]["client_ref"]
    asyncio.run(main_mod._create_gateway_session("mind-uuid"))

    assert first != sent["body"]["client_ref"]
