"""Every route the gateway serves is classified, and an unclassified one is
refused rather than served.

The table is only worth having if it cannot fall behind the app. A session
route added next month and never classified must fail here, because the
alternative — noticing in production — is how a route ships open.
"""

from __future__ import annotations

import os
import sys

from fastapi import FastAPI

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

from comms import session_access, user_access  # noqa: E402

# Invented values, never real credentials. Named once so the guard has one
# line to look at and the tests read as behaviour rather than as strings.
SHARED = "shared-token-for-tests"  # secret-guard: allow
ADMIN_TOKEN = "admin-token-for-tests"  # secret-guard: allow
SLOAN_TOKEN = "sloan-token-for-tests"  # secret-guard: allow
UNISSUED = "never-issued-for-tests"  # secret-guard: allow


def test_every_route_the_gateway_serves_is_classified():
    from comms import server

    assert session_access.TABLE.unlisted(server.app) == []


def test_the_table_classifies_nothing_the_gateway_does_not_serve():
    """A rule for a route that no longer exists is a rule nobody is reading,
    and it hides the fact that the route it was written for has moved."""
    from comms import server

    served = set()
    for route in server.app.router.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if methods:
            served |= {(m, path) for m in methods if m != "HEAD"}
        else:
            served.add((user_access.WEBSOCKET, path))

    assert sorted(set(session_access.TABLE.rules) - served) == []


def test_the_two_websocket_routes_are_classified_as_somebodys_conversation():
    """These are the routes an HTTP middleware cannot see. If either ever
    falls out of the table, the gate is silently absent on a whole
    conversation while every other test stays green.
    """
    for spec in (
        ("WS", "/sessions/{session_id}/stream"),
        ("WS", "/sessions/{session_id}/attach"),
    ):
        rule = session_access.TABLE.rules[spec]
        assert rule.kind == user_access.OWNER
        assert rule.param == "session_id"


def test_the_routes_that_took_only_the_admin_token_still_do():
    """The refactor must not have widened anything. Each of these answered to
    the admin token alone before the table existed."""
    for spec in (
        ("GET", "/sessions/live"),
        ("GET", "/sessions/live/text"),
        # `POST /broker/minds` deliberately left this set: a mind registers
        # itself, which is why every mind used to hold the admin token. It is
        # machinery now, and `test_a_mind_registers_only_itself` guards the
        # narrower rule the handler applies.
        ("PUT", "/broker/minds/{mind_id}/owner"),
        ("PUT", "/broker/minds/{name}"),
        ("DELETE", "/broker/minds/{name}"),
        ("POST", "/secrets/scopes"),
        ("DELETE", "/secrets/scopes"),
    ):
        assert session_access.TABLE.rules[spec].kind == user_access.ADMIN, spec


def test_registering_a_mind_is_machinery_rather_than_admin_only():
    """The one deliberate widening. A mind must be able to register itself —
    registration is the only channel by which the gateway learns that mind's
    session credential — and requiring the admin token for it is what put a
    skeleton key on every box in the house."""
    rule = session_access.TABLE.rules[("POST", "/broker/minds")]

    assert rule.kind == user_access.MACHINE


def test_filing_a_rotation_summary_is_machinery_rather_than_an_owned_conversation():
    """The second deliberate widening, and a repair. The rotation hook
    addresses this route by the id the harness gave it — a `claude_sid` —
    which is not a `sessions.id` and which the gate's owner check cannot
    resolve, so classifying it `OWNER` refused every mind on the hive and
    took rotation itself down with it: the hook returns before it arms, so a
    refused write means the conversation is never rotated at all. What the
    write is held to instead is the mind named in its body, checked against
    the caller's own holder inside the handler."""
    rule = session_access.TABLE.rules[
        ("POST", "/sessions/{session_id}/rotation-memory")
    ]

    assert rule.kind == user_access.MACHINE


def test_the_three_calls_a_mind_makes_about_its_own_conversation_are_machinery():
    """The seed a mind is owed when it opens a terminal, and the prose and
    activity it tails out of that terminal. Admin-only, these were the reason
    every mind on the hive had to hold a credential that opens every other
    mind. `_require_own_session_mind` then refuses everything but that
    conversation's own mind, so this class is a floor and not the whole
    check — a `service` holder passes the gate here and is refused by the
    handler."""
    for spec in (
        ("POST", "/sessions/{session_id}/pty-activity"),
        ("POST", "/sessions/{session_id}/pty-text"),
        ("GET", "/sessions/{session_id}/carry-forward"),
    ):
        assert session_access.TABLE.rules[spec].kind == user_access.MACHINE, spec


def test_a_route_added_with_no_classification_is_refused_to_a_non_admin(
    tmp_path, monkeypatch
):
    """Deny-by-default, driven rather than asserted about: a route the table
    has never heard of is added to a live app and answers 401 to a credential
    that is not the admin token."""
    import sqlite3
    from contextlib import asynccontextmanager

    import aiosqlite
    from fastapi.testclient import TestClient

    path = tmp_path / "broker.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE minds (mind_id TEXT PRIMARY KEY, name TEXT, owner TEXT);"
    )
    conn.executescript(user_access.CREDENTIALS_DDL)
    conn.execute(
        "INSERT INTO credentials "
        "(holder, username, role, token_sha, created_at) "
        "VALUES ('arnold', 'sloan', ?, ?, 0)",
        (user_access.MIND, user_access.digest(SLOAN_TOKEN)),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")

    state: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(_app):
        state["db"] = await aiosqlite.connect(path)
        yield
        await state["db"].close()

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/sessions/some-route-nobody-classified")
    async def forgotten():
        return {"served": True}

    table = user_access.RouteTable()
    app.add_middleware(
        user_access.UserAccess,
        table=table,
        broker_db=lambda: state.get("db"),
        session_mind=lambda _session_id: None,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        refused = client.get(
            "/sessions/some-route-nobody-classified",
            headers={"Authorization": "Bearer " + SLOAN_TOKEN},
        )
        served = client.get(
            "/sessions/some-route-nobody-classified",
            headers={"Authorization": "Bearer " + ADMIN_TOKEN},
        )

    assert refused.status_code == 401
    assert served.status_code == 200


def test_every_conversation_route_is_classified_as_somebodys_conversation():
    """The class each route got is the whole point of the table, and coverage
    tests do not check it: moving `POST /sessions/{id}/message` into the
    machinery block hands every user's message route back to the shared token
    and leaves a coverage-only suite green.
    """
    for method, path in (
        ("GET", "/sessions/{session_id}"),
        ("DELETE", "/sessions/{session_id}"),
        ("POST", "/sessions/{session_id}/suspend"),
        ("POST", "/sessions/{session_id}/resume"),
        ("POST", "/sessions/{session_id}/message"),
        ("GET", "/sessions/{session_id}/events"),
        ("GET", "/sessions/{session_id}/history"),
        ("POST", "/sessions/{session_id}/activate"),
        ("POST", "/sessions/{session_id}/model"),
        ("POST", "/sessions/{session_id}/autopilot"),
        ("POST", "/sessions/{session_id}/interrupt"),
        ("POST", "/sessions/{session_id}/remote-control"),
        ("DELETE", "/sessions/{session_id}/remote-control"),
        ("PUT", "/sessions/{session_id}/name"),
        ("WS", "/sessions/{session_id}/stream"),
        ("WS", "/sessions/{session_id}/attach"),
    ):
        rule = session_access.TABLE.rules[(method, path)]
        assert rule.kind == user_access.OWNER, (method, path, rule.kind)
        assert rule.param == "session_id", (method, path)


def test_the_listings_and_the_command_surface_are_classified_as_scoped():
    """Each of these filters its own answer, so it must reach the handler with
    a resolved caller rather than being refused or waved through."""
    for spec in (
        ("GET", "/sessions"),
        ("GET", "/sessions/names"),
        ("POST", "/sessions"),
        ("POST", "/command"),
    ):
        assert session_access.TABLE.rules[spec].kind == user_access.SCOPED, spec
