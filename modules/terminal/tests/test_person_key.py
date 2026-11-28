"""The terminal site calls the gateway as the person who asked, with the
sign-in key that came with that click and nothing else: not a shared service
token, not a key read from a file, not one kept from an earlier click."""

import json
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

import auth
import main as main_mod
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


def _sign_in(username="daniel"):
    """A real sign-in through the site's own sign-in page; returns the client
    and the key the browser now holds."""
    accounts.make_user(username, "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app, base_url="https://testserver")
    response = client.post("/login", data={"username": username, "password": "secret-pass",
                                           "next": "/"}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return client, client.cookies.get(auth.SESSION_COOKIE_NAME)


def test_gateway_call_carries_the_key_that_came_with_the_click_not_the_old_token(monkeypatch):
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "retired-shared-token")
    client, key = _sign_in()
    seen = _Seen()

    with patch("main.urllib.request.urlopen", seen):
        response = client.post("/api/terminal/sessions", json={"mind_id": "skippy-id"})

    assert response.status_code == 200
    assert len(seen.requests) == 1
    assert seen.requests[0].get_header("Authorization") == f"Bearer {key}"


def test_the_next_click_carries_that_clicks_key_and_the_site_remembers_the_first_nowhere(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_KEY_ROOT", str(tmp_path))
    first, first_key = _sign_in("daniel")
    second, second_key = _sign_in("sloan")
    seen = _Seen()

    with patch("main.urllib.request.urlopen", seen):
        first.post("/api/terminal/sessions", json={"mind_id": "skippy-id"})
        second.post("/api/terminal/sessions", json={"mind_id": "skippy-id"})

    assert [r.get_header("Authorization") for r in seen.requests] == [
        f"Bearer {first_key}", f"Bearer {second_key}"]
    assert os.listdir(tmp_path) == []  # nothing written where keys used to live
