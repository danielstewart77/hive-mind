"""The gateway takes a change for one mind, checks who is asking, delivers it.

Feature 834 follow-up, story 898 (protected behaviours B7, B8, B9). Driven
through the real gateway application over real rows; the mind is a small
server on the loopback (the transport is where the process stops being ours),
which records what it was sent.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient
from hive_data import roles

from comms import user_access
from comms_broker_pg import (  # noqa: F401 - fixtures
    broker_db, execute, fetchval, put_credential_row, put_mind, scratch_dsn,
)

ADMIN = "admin-bearer"  # secret-guard: allow
A, B, OP, FOREIGN = (
    "11111111-1111-4111-8111-11111111111%d" % i for i in (1, 2, 3, 4))
KEYS = {
    "alpha": "alpha-mind-key-for-tests",  # secret-guard: allow
    "beta": "beta-mind-key-for-tests",  # secret-guard: allow
    "ops": "ops-mind-key-for-tests",  # secret-guard: allow
    "other": "other-mind-key-for-tests",  # secret-guard: allow
    "daniel": "daniel-person-key-for-tests",  # secret-guard: allow
    "sloan": "sloan-person-key-for-tests",  # secret-guard: allow
    "root": "root-person-key-for-tests",  # secret-guard: allow
    "bot": "surface-bot-credential-for-tests",  # secret-guard: allow
}
SESSION_TOKEN = "alpha-gateway-credential-for-tests"  # secret-guard: allow


class _Mind(BaseHTTPRequestHandler):
    seen: list = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        type(self).seen.append({"method": self.command, "path": self.path,
                                "auth": self.headers.get("Authorization"), "body": body})
        payload = json.dumps({"applied": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_PUT = do_PATCH = do_POST = do_DELETE = _answer

    def log_message(self, *args):  # silent
        pass


@pytest.fixture
def mind_server():
    _Mind.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Mind)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _Mind.seen
    server.shutdown()


async def _seed(url: str) -> None:
    await execute('TRUNCATE core.group_members, core.groups, core.users CASCADE')
    await put_mind(A, "alpha", owner="daniel", gateway_url=url, session_token=SESSION_TOKEN)
    await put_mind(B, "beta", owner="daniel", gateway_url=url, session_token="beta-cred-for-tests")  # secret-guard: allow
    await put_mind(OP, "ops", owner="daniel", gateway_url=url, session_token="ops-cred-for-tests")  # secret-guard: allow
    await put_mind(FOREIGN, "other", owner="sloan", gateway_url=url, session_token="other-cred-for-tests")  # secret-guard: allow
    for holder, owner in (("alpha", "daniel"), ("beta", "daniel"),
                          ("ops", "daniel"), ("other", "sloan")):
        await put_credential_row(holder, owner, user_access.MIND, user_access.digest(KEYS[holder]))
    await put_credential_row("surface-bot", "daniel", user_access.SERVICE,
                             user_access.digest(KEYS["bot"]))
    for person in ("daniel", "sloan", "root"):
        await put_credential_row(person, person, user_access.USER_KEY,
                                 user_access.digest(KEYS[person]))
        await execute(
            "INSERT INTO core.users (username, password_hash, created_at) VALUES ($1, 'x', 0)",
            person)
    await execute("INSERT INTO core.groups (name, created_at) VALUES ('admins', 0) ON CONFLICT DO NOTHING")
    await execute(
        "INSERT INTO core.group_members (group_name, user_id) "
        "SELECT 'admins', id FROM core.users WHERE username = 'root'")


@pytest.fixture
def world(monkeypatch, broker_db, mind_server):
    url, seen = mind_server
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN)
    from comms import server

    importlib.reload(server)
    asyncio.run(_seed(url))
    with TestClient(server.app) as client:
        async def _role() -> int:
            found = await roles.role_by_slug("operator")
            if found:
                return found["id"]
            return (await roles.create_role("Operator", slug="operator"))["row"]["id"]

        role_id = client.portal.call(_role)
        yield type("World", (), dict(
            client=client, seen=seen,
            make_operator=staticmethod(lambda: client.portal.call(roles.set_mind_roles, OP, [role_id])),
            lose_role=staticmethod(lambda: client.portal.call(roles.set_mind_roles, OP, [])),
            drop_admin=staticmethod(lambda: client.portal.call(
                execute, "DELETE FROM core.group_members WHERE group_name = 'admins'")),
        ))
        client.portal.call(roles.set_mind_roles, OP, [])


def _change(world, key, mind="alpha", tail="runtime", method="PATCH", body=None):
    return world.client.request(
        method, f"/broker/minds/{mind}/forward/{tail}",
        headers={"Authorization": f"Bearer {key}"}, json=body or {"model": "x"})


def test_a_change_from_the_person_who_owns_the_mind_is_delivered_and_a_stranger_is_refused(world) -> None:
    assert _change(world, KEYS["daniel"]).status_code == 200
    assert [s["path"] for s in world.seen] == ["/runtime"]
    refused = _change(world, KEYS["sloan"])
    assert refused.status_code == 403
    assert len(world.seen) == 1  # the refused request never reached the mind


def test_the_mind_is_sent_its_own_gateway_credential_and_never_the_askers_key(world) -> None:
    _change(world, KEYS["daniel"])
    assert world.seen[0]["auth"] == f"Bearer {SESSION_TOKEN}"
    assert KEYS["daniel"] not in json.dumps(world.seen)


def test_an_administrator_changes_any_mind_and_a_person_who_is_not_one_is_refused(world) -> None:
    assert _change(world, KEYS["root"], mind="other").status_code == 200
    assert _change(world, KEYS["root"], mind="alpha").status_code == 200
    assert _change(world, KEYS["sloan"], mind="alpha").status_code == 403
    assert len(world.seen) == 2


def test_a_person_removed_from_the_admins_group_is_refused_on_the_next_request(world) -> None:
    assert _change(world, KEYS["root"], mind="other").status_code == 200
    world.drop_admin()
    assert _change(world, KEYS["root"], mind="other").status_code == 403
    assert len(world.seen) == 1


def test_the_gateways_own_credential_and_a_value_that_is_no_key_are_refused_on_this_route(world) -> None:
    for value in (ADMIN, KEYS["bot"], "never-issued-for-tests", ""):
        assert _change(world, value).status_code == 401
    assert world.seen == []


def test_an_operator_mind_changes_a_mind_of_its_owner_and_an_ordinary_mind_only_itself(world) -> None:
    assert _change(world, KEYS["beta"], mind="alpha").status_code == 403
    assert _change(world, KEYS["alpha"], mind="alpha").status_code == 200
    world.make_operator()
    assert _change(world, KEYS["ops"], mind="alpha").status_code == 200
    assert _change(world, KEYS["ops"], mind="other").status_code == 403
    world.lose_role()
    assert _change(world, KEYS["ops"], mind="alpha").status_code == 403
    assert len(world.seen) == 2


def test_only_the_listed_settings_routes_are_forwarded_never_a_conversation(world) -> None:
    for tail in ("sessions", "sessions/abc/message", "runtime/../sessions", "runtime/./x", "files//content"):
        assert _change(world, KEYS["daniel"], tail=tail, method="POST").status_code == 404
    assert world.seen == []


def test_a_mind_that_cannot_be_reached_is_said_so_plainly(world, mind_server) -> None:
    asyncio.run(execute("UPDATE core.minds SET gateway_url = 'http://127.0.0.1:1' WHERE name = 'alpha'"))
    answer = _change(world, KEYS["daniel"])
    assert answer.status_code == 502
    assert "could not be reached" in answer.json()["detail"]
