"""What the gateway shows a mind is made from the mind's one key.

Feature 834 follow-up, story 902 (protected behaviour B13, the gateway's half):
the gateway works the value out from the key a mind registers with or is given,
keeps that value and never the key, carries it on its next call, carries the new
one after the key is replaced, and does not call a mind whose key was cut off.
Driven through the real gateway application over real rows; the mind is a small
server on the loopback which records what it was shown.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from comms import user_access
from comms_broker_pg import (  # noqa: F401 - fixtures
    broker_db, execute, fetchval, put_credential_row, scratch_dsn,
)

ADMIN = "admin-bearer"  # secret-guard: allow
MIND = "11111111-1111-4111-8111-111111111111"
KEY = "alpha-one-key-for-tests-0001"  # secret-guard: allow
NEW_KEY = "alpha-one-key-for-tests-0002"  # secret-guard: allow
PERSON = "daniel-person-key-for-tests"  # secret-guard: allow


class _Mind(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        type(self).seen.append(self.headers.get("Authorization"))
        payload = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def world(monkeypatch, broker_db):
    _Mind.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Mind)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN)
    from comms import server as gateway

    importlib.reload(gateway)
    asyncio.run(execute("TRUNCATE core.group_members, core.groups, core.users CASCADE"))
    asyncio.run(put_credential_row("daniel", "daniel", user_access.USER_KEY,
                                   user_access.digest(PERSON)))
    asyncio.run(execute(
        "INSERT INTO core.users (username, password_hash, created_at) VALUES ('daniel', 'x', 0)"))
    with TestClient(gateway.app) as client:
        yield type("World", (), dict(
            client=client, seen=_Mind.seen, url=f"http://127.0.0.1:{server.server_port}"))
    server.shutdown()


def _register(world, key, **extra):
    body = {"mind_id": MIND, "name": "alpha", "gateway_url": world.url,
            "model": "sonnet", "harness": "claude_cli", "owner": "daniel", **extra}
    return world.client.post("/broker/minds", json=body,
                             headers={"Authorization": f"Bearer {key}"})


def _mint(world, key):
    return world.client.put(
        "/credentials/alpha", json={"username": "daniel", "token": key, "role": "mind"},
        headers={"Authorization": f"Bearer {ADMIN}"})


def _call_mind(world):
    return world.client.get("/broker/minds/alpha/forward/health",
                            headers={"Authorization": f"Bearer {PERSON}"})


def _stored():
    return asyncio.run(fetchval("SELECT session_token FROM core.minds WHERE mind_id = $1", MIND))


def test_a_mind_registering_with_its_key_is_shown_a_value_made_from_it_and_the_key_is_kept_nowhere(world) -> None:
    assert _mint(world, KEY).status_code == 200
    assert _register(world, KEY).status_code == 200
    assert _stored() == user_access.gateway_credential(KEY)
    assert _call_mind(world).status_code == 200
    assert world.seen == [f"Bearer {user_access.gateway_credential(KEY)}"]
    assert KEY not in json.dumps(world.seen)
    assert asyncio.run(fetchval(
        "SELECT count(*) FROM core.minds WHERE session_token = $1", KEY)) == 0


def test_after_the_key_is_replaced_the_next_call_carries_the_new_value_with_no_step_by_a_person(world) -> None:
    _mint(world, KEY)
    _register(world, KEY)
    assert _call_mind(world).status_code == 200
    assert _mint(world, NEW_KEY).status_code == 200
    assert _call_mind(world).status_code == 200
    assert world.seen == [f"Bearer {user_access.gateway_credential(KEY)}",
                          f"Bearer {user_access.gateway_credential(NEW_KEY)}"]


def test_a_mind_that_has_been_cut_off_is_not_called(world) -> None:
    _mint(world, KEY)
    _register(world, KEY)
    assert world.client.delete(
        "/credentials/alpha", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200
    answer = _call_mind(world)
    assert answer.status_code == 502
    assert world.seen == []


def test_the_calculated_value_presented_as_a_key_is_refused(world) -> None:
    _mint(world, KEY)
    _register(world, KEY)
    shown = user_access.gateway_credential(KEY)
    for path, method in (("/broker/minds", "GET"), ("/credentials", "GET")):
        assert world.client.request(
            method, path, headers={"Authorization": f"Bearer {shown}"}).status_code in (401, 403)
    assert world.client.post(
        "/broker/minds", json={"mind_id": MIND, "name": "alpha", "gateway_url": world.url,
                               "model": "m", "harness": "claude_cli"},
        headers={"Authorization": f"Bearer {shown}"}).status_code == 401
