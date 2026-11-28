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
    status = 200

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        type(self).seen.append({"method": self.command, "path": self.path,
                                "auth": self.headers.get("Authorization"), "body": body})
        payload = json.dumps({"applied": True}).encode()
        self.send_response(type(self).status)
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


def test_a_disabled_person_changes_nothing_and_a_mind_with_an_odd_address_is_not_called(world) -> None:
    asyncio.run(execute("UPDATE core.users SET disabled_at = 1 WHERE username = 'daniel'"))
    assert _change(world, KEYS["daniel"]).status_code == 401
    asyncio.run(execute("UPDATE core.users SET disabled_at = NULL WHERE username = 'daniel'"))
    for odd in ("http://127.0.0.1:9/latest/#", "http://127.0.0.1:9/?x=", "file:///etc/passwd",
                "http://u:p@127.0.0.1:9"):
        asyncio.run(execute("UPDATE core.minds SET gateway_url = $1 WHERE name = 'alpha'", odd))
        assert _change(world, KEYS["daniel"]).status_code == 502
    assert world.seen == []


def test_the_minds_answer_status_and_body_come_back_and_what_was_sent_arrives_whole(world) -> None:
    answer = _change(world, KEYS["daniel"], tail="files/skills/content", method="PUT",
                     body={"text": "hello"})
    assert answer.status_code == 200 and answer.json() == {"applied": True}
    assert world.seen[0]["method"] == "PUT"
    assert world.seen[0]["path"] == "/files/skills/content"
    assert json.loads(world.seen[0]["body"]) == {"text": "hello"}


def test_a_mind_that_refuses_the_gateways_credential_is_reported_as_out_of_date(world) -> None:
    _Mind.status = 401
    try:
        answer = _change(world, KEYS["daniel"])
    finally:
        _Mind.status = 200
    assert answer.status_code == 502
    assert "refreshes when the mind next registers" in answer.json()["detail"]


def test_a_mind_the_gateway_holds_no_credential_for_is_told_to_register_again(world) -> None:
    asyncio.run(execute("UPDATE core.minds SET session_token = NULL WHERE name = 'alpha'"))
    answer = _change(world, KEYS["daniel"])
    assert answer.status_code == 409
    assert "register again" in answer.json()["detail"]
    assert world.seen == []


def test_a_mind_with_no_owner_answers_to_the_primary_user_only(world, monkeypatch) -> None:
    monkeypatch.setenv("COMMS_PRIMARY_USER", "sloan")
    asyncio.run(execute("UPDATE core.minds SET owner = NULL WHERE name = 'alpha'"))
    assert _change(world, KEYS["daniel"]).status_code == 403
    assert _change(world, KEYS["sloan"]).status_code == 200


def test_a_traversal_that_would_land_on_an_allowed_route_is_still_refused(world) -> None:
    for tail in ("runtime/%2e%2e/runtime", "runtime/", "runtime//settings"):
        assert _change(world, KEYS["daniel"], tail=tail, method="GET").status_code == 404
    assert world.seen == []


def _as(world, key, method, path, **kw):
    return world.client.request(method, path, headers={"Authorization": f"Bearer {key}"}, **kw)


def test_a_persons_key_lists_and_changes_only_their_own_minds_and_an_administrators_key_all(world) -> None:
    """B12 (story 905): a person's key who is not an administrator reaches
    their own minds and is refused another person's; an administrator's key
    reaches every mind; the gateway's own credential is not a person."""
    def names(key):
        return {m["name"] for m in _as(world, key, "GET", "/broker/minds").json()}

    assert names(KEYS["daniel"]) == {"alpha", "beta", "ops"}
    assert names(KEYS["sloan"]) == {"other"}
    assert names(KEYS["root"]) == {"alpha", "beta", "ops", "other"}
    # A change to a mind row: the owner's key, an administrator's key; not a stranger's.
    assert _as(world, KEYS["daniel"], "PUT", "/broker/minds/other", json={"voice": "v"}).status_code == 401
    assert _as(world, KEYS["sloan"], "PUT", "/broker/minds/other", json={"voice": "v"}).status_code == 200
    assert _as(world, KEYS["root"], "PUT", "/broker/minds/alpha", json={"voice": "v"}).status_code == 200
    # Moving a mind between people and reading every conversation's text: administrators only.
    assert _as(world, KEYS["daniel"], "PUT", "/broker/minds/alpha/owner", json={"owner": "sloan"}).status_code == 401
    assert _as(world, KEYS["daniel"], "GET", "/sessions/live/text?session_id=x").status_code == 401
    assert _as(world, KEYS["root"], "GET", "/sessions/live/text?session_id=x").status_code == 200


def _scope(world, key, mind, method="POST"):
    return world.client.request(
        method, "/secrets/scopes", headers={"Authorization": f"Bearer {key}"},
        json={"mind_name": mind, "secret_keys": ["SOME_SECRET"]})


def test_secret_scopes_are_granted_by_the_minds_owner_or_an_administrator_and_by_no_mind(world) -> None:
    """The secret scope routes take a person's own key: the owner of the mind
    or an administrator. A mind's key (even the mind's own), another person
    and a service are refused."""
    assert _scope(world, KEYS["daniel"], "alpha").status_code == 200
    assert _scope(world, KEYS["root"], "other").status_code == 200
    assert _scope(world, KEYS["daniel"], "alpha", method="DELETE").status_code == 200
    for refused in (_scope(world, KEYS["sloan"], "alpha"),
                    _scope(world, KEYS["alpha"], "alpha"),
                    _scope(world, KEYS["ops"], "beta"),
                    _scope(world, KEYS["bot"], "alpha"),
                    _scope(world, KEYS["daniel"], "other")):
        assert refused.status_code in (401, 404)
