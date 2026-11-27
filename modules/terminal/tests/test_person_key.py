"""The terminal site calls the gateway as the person who asked, with that
person's own key from the key folder, never with a shared service token."""

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

import auth
import main as main_mod
import person_key
import person_key_support
from hive_site import testing as accounts


class _Seen:
    """The transport: records the request that would have left for the gateway."""

    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"id": "new-session"}).encode("utf-8")


def _client(username="daniel"):
    user = accounts.make_user(username, "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


def test_gateway_call_carries_the_signed_in_persons_key_not_the_old_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "retired-shared-token")
    key = person_key_support.give_key(tmp_path, monkeypatch, "daniel")
    seen = _Seen()

    with patch("main.urllib.request.urlopen", seen):
        response = _client().post("/api/terminal/sessions", json={"mind_id": "skippy-id"})

    assert response.status_code == 200
    assert len(seen.requests) == 1
    assert seen.requests[0].get_header("Authorization") == f"Bearer {key}"


def test_a_person_with_no_key_is_told_to_open_the_hive_site_with_a_409(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    empty = tmp_path / "empty-key-folder"
    empty.mkdir()
    monkeypatch.setenv("HIVE_KEY_ROOT", str(empty))
    seen = _Seen()

    with patch("main.urllib.request.urlopen", seen):
        response = _client().post("/api/terminal/sessions", json={"mind_id": "skippy-id"})

    assert response.status_code == 409
    assert response.json()["detail"] == person_key.NO_KEY_SENTENCE
    assert "hive site" in response.json()["detail"]
    assert seen.requests == []
