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
        ("POST", "/sessions/{session_id}/pty-activity"),
        ("POST", "/sessions/{session_id}/pty-text"),
        ("GET", "/sessions/{session_id}/carry-forward"),
        ("POST", "/broker/minds"),
        ("PUT", "/broker/minds/{mind_id}/owner"),
        ("PUT", "/broker/minds/{name}"),
        ("DELETE", "/broker/minds/{name}"),
        ("POST", "/secrets/scopes"),
        ("DELETE", "/secrets/scopes"),
    ):
        assert session_access.TABLE.rules[spec].kind == user_access.ADMIN, spec


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
        "INSERT INTO user_credentials (username, token_sha, created_at) "
        "VALUES ('sloan', ?, 0)",
        (user_access.digest(SLOAN_TOKEN),),
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
