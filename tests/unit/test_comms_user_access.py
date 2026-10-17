"""Who is asking comms, and which conversations answer them.

A conversation's owner is derived — session names a mind, `broker.minds` names
the mind's owner — and the gate in front of every session route is the thing
that has to agree with that derivation on every path, websockets included.
"""

from __future__ import annotations

import os
import sys

import aiosqlite
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

# Each test file needing the gateway package puts `nervous-system` on the
# path itself; it holds a hyphen, so it can never be a package name.
NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

from comms import user_access as ua


# ---------------------------------------------------------------------------
# Fixtures: a broker db holding two users' minds
# ---------------------------------------------------------------------------

MINDS_DDL = """
CREATE TABLE minds (
    mind_id TEXT PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    owner   TEXT
);
"""

# Invented values, never real credentials. Named once so the guard has one
# line to look at and the tests read as behaviour rather than as strings.
SHARED = "shared-token-for-tests"  # secret-guard: allow
ADMIN_TOKEN = "admin-token-for-tests"  # secret-guard: allow
SLOAN_TOKEN = "sloan-token-for-tests"  # secret-guard: allow
UNISSUED = "never-issued-for-tests"  # secret-guard: allow

DANIEL_MIND = "14cb820b-4a42-4f04-a593-54f532fd1d2f"
SLOAN_MIND = "9f3c1a77-0000-4000-8000-0000000000aa"


@pytest.fixture()
async def db(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "broker.db")
    await conn.executescript(MINDS_DDL)
    await conn.executemany(
        "INSERT INTO minds (mind_id, name, owner) VALUES (?, ?, ?)",
        [
            (DANIEL_MIND, "skippy", "daniel"),
            (SLOAN_MIND, "arnold", "sloan"),
            ("3b0e0000-0000-4000-8000-0000000000bb", "orphan", None),
        ],
    )
    await conn.commit()
    await ua.init_credentials(conn)
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """The three environment values this module reads are the subject of
    several of these tests; an ambient one would mask them."""
    for name in (
        "COMMS_BEARER_TOKEN",
        "COMMS_ADMIN_BEARER_TOKEN",
        "COMMS_PRIMARY_USER",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# T1 — the registry mints a credential that verifies, and revoking kills it
# ---------------------------------------------------------------------------


async def test_a_minted_credential_verifies_and_an_unminted_value_does_not(db):
    await ua.put_credential(db, "sloan", SLOAN_TOKEN)

    assert await ua.user_for_token(db, SLOAN_TOKEN) == "sloan"
    assert await ua.user_for_token(db, UNISSUED) is None


async def test_a_credential_is_stored_as_a_digest_and_never_in_the_clear(db):
    await ua.put_credential(db, "sloan", SLOAN_TOKEN)

    rows = await db.execute("SELECT token_sha FROM user_credentials")
    stored = [r[0] for r in await rows.fetchall()]

    assert stored == [ua.digest(SLOAN_TOKEN)]
    assert SLOAN_TOKEN not in stored


# ---------------------------------------------------------------------------
# T2 — revocation lands on the next request, with no restart
# ---------------------------------------------------------------------------


async def test_revoking_a_credential_stops_it_verifying_in_the_same_process(db):
    await ua.put_credential(db, "sloan", SLOAN_TOKEN)
    assert await ua.user_for_token(db, SLOAN_TOKEN) == "sloan"

    assert await ua.revoke_credential(db, "sloan") is True

    # Same connection, same process, no re-import: a cache anywhere in the
    # read path would leave this returning "sloan".
    assert await ua.user_for_token(db, SLOAN_TOKEN) is None


async def test_revoking_a_credential_nobody_minted_reports_that_it_did_nothing(db):
    assert await ua.revoke_credential(db, "nobody") is False


async def test_reissuing_a_credential_clears_an_earlier_revocation(db):
    await ua.put_credential(db, "sloan", "first-value")
    await ua.revoke_credential(db, "sloan")

    await ua.put_credential(db, "sloan", "second-value")

    assert await ua.user_for_token(db, "second-value") == "sloan"
    assert await ua.user_for_token(db, "first-value") is None


# ---------------------------------------------------------------------------
# T8 — the existing shared token is the primary user's own credential
# ---------------------------------------------------------------------------


async def test_the_shared_token_is_the_named_primary_user(db, monkeypatch):
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")

    caller = await ua.resolve_caller(db, SHARED)

    assert caller.username == "daniel"
    assert caller.sees_everything() is False


async def test_the_shared_token_reaches_everything_when_no_primary_user_is_named(
    db, monkeypatch
):
    """The rollout switch. A hive upgrading with no new configuration behaves
    exactly as the one it replaced."""
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)

    caller = await ua.resolve_caller(db, SHARED)

    assert caller.sees_everything() is True


async def test_the_admin_token_reaches_everything(db, monkeypatch):
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")

    caller = await ua.resolve_caller(db, ADMIN_TOKEN)

    assert caller.is_admin is True
    assert caller.sees_everything() is True


async def test_an_unknown_value_is_not_recognised(db, monkeypatch):
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)

    caller = await ua.resolve_caller(db, UNISSUED)

    assert caller.recognised is False


async def test_a_user_credential_is_resolved_to_its_own_user(db, monkeypatch):
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")
    await ua.put_credential(db, "sloan", SLOAN_TOKEN)

    caller = await ua.resolve_caller(db, SLOAN_TOKEN)

    assert caller.username == "sloan"
    assert caller.sees_everything() is False


# ---------------------------------------------------------------------------
# Ownership derivation
# ---------------------------------------------------------------------------


async def test_a_mind_resolves_to_its_owner_by_uuid_and_by_name(db):
    assert await ua.owner_of_mind(db, DANIEL_MIND) == "daniel"
    assert await ua.owner_of_mind(db, "skippy") == "daniel"
    assert await ua.owner_of_mind(db, SLOAN_MIND) == "sloan"


async def test_a_mind_with_no_owner_and_a_mind_that_is_not_registered_resolve_to_none(
    db,
):
    assert await ua.owner_of_mind(db, "orphan") is None
    assert await ua.owner_of_mind(db, "no-such-mind") is None


async def test_the_keys_a_caller_owns_cover_both_the_uuid_and_the_name(db):
    keys = await ua.visible_minds(db, ua.Caller(username="daniel"))

    assert DANIEL_MIND in keys
    assert "skippy" in keys
    assert SLOAN_MIND not in keys
    assert "arnold" not in keys


async def test_a_listing_keeps_only_the_callers_own_conversations(db):
    keys = await ua.visible_minds(db, ua.Caller(username="sloan"))
    rows = [
        {"id": "a", "mind_id": DANIEL_MIND},
        {"id": "b", "mind_id": SLOAN_MIND},
        {"id": "c", "mind_id": "skippy"},
    ]

    kept = ua.filter_sessions(rows, ua.Caller(username="sloan"), keys)

    assert [r["id"] for r in kept] == ["b"]


async def test_an_admin_listing_keeps_every_conversation(db):
    caller = ua.Caller(is_admin=True, unscoped=True)
    keys = await ua.visible_minds(db, caller)
    rows = [{"id": "a", "mind_id": DANIEL_MIND}, {"id": "b", "mind_id": SLOAN_MIND}]

    assert len(ua.filter_sessions(rows, caller, keys)) == 2


# ---------------------------------------------------------------------------
# T9, T10 — classification is deny-by-default and names every route
# ---------------------------------------------------------------------------


def test_an_unclassified_route_falls_to_admin_only():
    table = ua.RouteTable()

    assert table.rule_for("GET", "/anything-at-all") == ua.Rule(ua.ADMIN)


def test_a_route_classified_twice_is_refused_at_build_time():
    table = ua.RouteTable()
    table.add(ua.Rule(ua.MACHINE), "POST /sessions/context")

    with pytest.raises(ValueError, match="classified twice"):
        table.add(ua.Rule(ua.SCOPED), "POST /sessions/context")


def test_an_owner_rule_without_a_parameter_is_refused_at_build_time():
    with pytest.raises(ValueError, match="names the parameter"):
        ua.Rule(ua.OWNER)


def test_an_unknown_route_class_is_refused_at_build_time():
    with pytest.raises(ValueError, match="unknown route class"):
        ua.Rule("sort-of-public")


def test_head_is_answered_by_the_get_rule():
    table = ua.RouteTable()
    table.add(ua.Rule(ua.PUBLIC), "GET /health")

    assert table.rule_for("HEAD", "/health").kind == ua.PUBLIC


def test_unlisted_names_every_route_the_table_does_not_including_websockets():
    app = FastAPI()

    @app.get("/classified")
    async def classified():  # pragma: no cover - never called
        return {}

    @app.post("/forgotten")
    async def forgotten():  # pragma: no cover - never called
        return {}

    @app.websocket("/forgotten-socket")
    async def forgotten_socket(ws: WebSocket):  # pragma: no cover - never called
        await ws.accept()

    table = ua.RouteTable()
    table.add(ua.Rule(ua.PUBLIC), "GET /classified")

    missing = table.unlisted(app)

    assert ("POST", "/forgotten") in missing
    assert (ua.WEBSOCKET, "/forgotten-socket") in missing
    assert ("GET", "/classified") not in missing


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def _gated_app(broker_path, mind_of: dict[str, str]):
    """A small app wearing the real gate, so these tests drive the shipped
    middleware rather than a re-implementation of its decisions.

    Docs routes are off for the same reason comms turns them off, which also
    keeps `unlisted` about the routes this app actually declares.
    """
    from contextlib import asynccontextmanager

    state: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(_app):
        # Opened on the loop the app runs on. An aiosqlite connection made on
        # another loop is the quiet way these tests would hang.
        state["db"] = (
            await aiosqlite.connect(broker_path) if broker_path else None
        )
        yield
        if state["db"] is not None:
            await state["db"].close()

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/sessions")
    async def listing():
        return {"listed": True}

    # Literal paths before the parameterised one, the same order comms
    # declares them in: otherwise "/sessions/live" matches
    # "/sessions/{session_id}" and is classified as somebody's conversation.
    @app.post("/sessions/context")
    async def machinery():
        return {"recorded": True}

    @app.get("/sessions/live")
    async def live():
        return {"live": True}

    @app.get("/sessions/{session_id}")
    async def one(session_id: str):
        return {"session_id": session_id}

    @app.websocket("/sessions/{session_id}/attach")
    async def attach(ws: WebSocket, session_id: str):
        await ws.accept()
        await ws.send_text("attached")
        await ws.close()

    table = ua.RouteTable()
    table.add(ua.Rule(ua.PUBLIC), "GET /health")
    table.add(ua.Rule(ua.SCOPED), "GET /sessions")
    table.add(ua.Rule(ua.MACHINE), "POST /sessions/context")
    table.add(ua.Rule(ua.ADMIN), "GET /sessions/live")
    table.add(ua.Rule(ua.OWNER, param="session_id"), "GET /sessions/{session_id}")
    table.add(
        ua.Rule(ua.OWNER, param="session_id"), "WS /sessions/{session_id}/attach"
    )

    async def session_mind(session_id: str):
        return mind_of.get(session_id)

    app.add_middleware(
        ua.UserAccess,
        table=table,
        broker_db=lambda: state.get("db"),
        session_mind=session_mind,
    )
    return app, table


@pytest.fixture()
def broker_path(tmp_path):
    """The broker db as a file, built synchronously so the app can own the
    only async connection to it."""
    import sqlite3

    path = tmp_path / "broker.db"
    conn = sqlite3.connect(path)
    conn.executescript(MINDS_DDL)
    conn.executescript(ua.CREDENTIALS_DDL)
    conn.executemany(
        "INSERT INTO minds (mind_id, name, owner) VALUES (?, ?, ?)",
        [
            (DANIEL_MIND, "skippy", "daniel"),
            (SLOAN_MIND, "arnold", "sloan"),
        ],
    )
    # Constructing an input. The digest still comes from the module, so a
    # change to how a credential is stored fails these tests rather than
    # passing against a hash this file computed its own way.
    conn.execute(
        "INSERT INTO user_credentials (username, token_sha, created_at) "
        "VALUES (?, ?, 0)",
        ("sloan", ua.digest(SLOAN_TOKEN)),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def gated(broker_path, monkeypatch):
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")
    app, table = _gated_app(
        broker_path, {"d-session": DANIEL_MIND, "s-session": SLOAN_MIND}
    )
    assert table.unlisted(app) == []
    return app


def _get(app, path, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.get(path, headers=headers)


def test_a_public_route_needs_no_credential(gated):
    assert _get(gated, "/health").status_code == 200


def test_an_unrecognised_credential_is_refused_on_a_session_route(gated):
    assert _get(gated, "/sessions", token=UNISSUED).status_code == 401


def test_a_session_route_with_no_credential_at_all_is_refused(gated):
    assert _get(gated, "/sessions").status_code == 401


def test_a_caller_reaching_their_own_conversation_is_served(gated):
    answer = _get(gated, "/sessions/s-session", token=SLOAN_TOKEN)

    assert answer.status_code == 200
    assert answer.json() == {"session_id": "s-session"}


def test_another_users_conversation_answers_exactly_as_an_absent_one(gated):
    """The anti-probing requirement: the two answers are the same bytes, so
    the existence of someone else's conversation cannot be inferred."""
    theirs = _get(gated, "/sessions/d-session", token=SLOAN_TOKEN)
    absent = _get(gated, "/sessions/no-such-session", token=SLOAN_TOKEN)

    assert theirs.status_code == absent.status_code == 404
    assert theirs.content == absent.content


def test_the_primary_users_shared_token_reaches_their_own_conversation(gated):
    assert (
        _get(gated, "/sessions/d-session", token=SHARED).status_code == 200
    )


def test_the_primary_users_shared_token_is_refused_another_users_conversation(gated):
    assert (
        _get(gated, "/sessions/s-session", token=SHARED).status_code == 404
    )


def test_naming_no_primary_user_leaves_the_shared_token_reaching_everything(
    broker_path, monkeypatch
):
    """The rollout switch, through the gate: a hive that upgrades to this code
    and sets nothing new behaves exactly as the one it replaced."""
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.delenv("COMMS_PRIMARY_USER", raising=False)
    app, _ = _gated_app(
        broker_path, {"d-session": DANIEL_MIND, "s-session": SLOAN_MIND}
    )

    assert _get(app, "/sessions/s-session", token=SHARED).status_code == 200
    assert _get(app, "/sessions/d-session", token=SHARED).status_code == 200


def test_the_admin_token_reaches_every_users_conversation(gated):
    assert _get(gated, "/sessions/d-session", token=ADMIN_TOKEN).status_code == 200
    assert _get(gated, "/sessions/s-session", token=ADMIN_TOKEN).status_code == 200


def test_an_admin_only_route_refuses_a_user_credential(gated):
    assert _get(gated, "/sessions/live", token=SLOAN_TOKEN).status_code == 401
    assert _get(gated, "/sessions/live", token=ADMIN_TOKEN).status_code == 200


def test_the_machinery_routes_still_serve_the_shared_token(gated):
    with TestClient(gated, raise_server_exceptions=False) as client:
        answer = client.post(
            "/sessions/context", headers={"Authorization": "Bearer " + SHARED}
        )

    assert answer.status_code == 200


def test_a_revoked_credential_is_refused_by_the_gate_on_the_very_next_request(
    gated, broker_path
):
    """The gate consults the table per request. A cache anywhere in that read
    path leaves the second call answering 200 with nothing restarted."""
    import asyncio

    async def revoke():
        conn = await aiosqlite.connect(broker_path)
        try:
            await ua.revoke_credential(conn, "sloan")
        finally:
            await conn.close()

    with TestClient(gated, raise_server_exceptions=False) as client:
        headers = {"Authorization": "Bearer " + SLOAN_TOKEN}
        before = client.get("/sessions/s-session", headers=headers)
        asyncio.run(revoke())
        after = client.get("/sessions/s-session", headers=headers)

    assert before.status_code == 200
    # Refused as an unrecognised caller rather than as a missing conversation:
    # the credential died, which is a different fact from the conversation not
    # being theirs, and the holder needs to be told which.
    assert after.status_code == 401


def test_a_websocket_from_another_user_is_refused_with_a_real_status(gated):
    """The refusal has to be an HTTP status through the ASGI denial-response
    extension, not a pre-accept close. A close presents to the browser as 403,
    which is also what a gateway with no attach route answers — so a refused
    credential would read as a missing route and send the operator off to
    redeploy something that is working."""
    from starlette.testclient import WebSocketDenialResponse

    with TestClient(gated) as client:
        with pytest.raises(WebSocketDenialResponse) as refused:
            with client.websocket_connect(
                "/sessions/d-session/attach",
                headers={"Authorization": "Bearer " + SLOAN_TOKEN},
            ):
                pass

    assert refused.value.status_code == 404


def test_a_websocket_with_no_credential_is_refused_with_a_real_status(gated):
    from starlette.testclient import WebSocketDenialResponse

    with TestClient(gated) as client:
        with pytest.raises(WebSocketDenialResponse) as refused:
            with client.websocket_connect("/sessions/s-session/attach"):
                pass

    assert refused.value.status_code == 401


def test_a_websocket_to_a_callers_own_conversation_is_accepted(gated):
    with TestClient(gated) as client:
        with client.websocket_connect(
            "/sessions/s-session/attach",
            headers={"Authorization": "Bearer " + SLOAN_TOKEN},
        ) as ws:
            assert ws.receive_text() == "attached"


def test_the_gate_refuses_rather_than_serving_with_no_database(monkeypatch):
    """Between process start and the broker connection opening there is a
    window. Passing a session route through it unresolved is the one thing
    this gate must never do."""
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    app, _ = _gated_app(None, {})

    assert _get(app, "/sessions", token=SHARED).status_code == 503


# --- the credential classes are not interchangeable -----------------------


async def test_the_shared_token_is_both_the_primary_user_and_the_machinery(
    db, monkeypatch
):
    """It is two things at once, and conflating them is how a person's
    credential got admitted to every machinery route."""
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")

    caller = await ua.resolve_caller(db, SHARED)

    assert caller.username == "daniel"
    assert caller.is_machine is True
    assert caller.sees_everything() is False


async def test_a_persons_credential_is_recognised_but_is_not_machinery(db, monkeypatch):
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")
    await ua.put_credential(db, "sloan", SLOAN_TOKEN)

    caller = await ua.resolve_caller(db, SLOAN_TOKEN)

    assert caller.recognised is True
    assert caller.is_machine is False


async def test_bypass_mode_serves_a_caller_that_presented_a_stale_token(
    db, monkeypatch
):
    """An unset service token has always meant no gating, and master returned
    before even reading the header. Gating that on an empty *token* instead
    took a host whose comms has no service token, while a surface still holds
    a stale one, from fully served to fully refused."""
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)

    presented = await ua.resolve_caller(db, "a-stale-value-a-bot-still-holds")
    nothing = await ua.resolve_caller(db, "")

    assert presented.sees_everything() is True
    assert nothing.sees_everything() is True


# --- a conversation whose mind has no owner -------------------------------


async def test_an_ownerless_conversation_answers_to_the_primary_user(
    db, monkeypatch
):
    """`mind_id` carried a `DEFAULT 'ada'` for most of this schema's life, and
    a mind row's `owner` is nullable. Refusing those to everyone would lose
    the operator their own history the moment scoping is switched on."""
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")

    assert await ua.owns_mind(db, ua.Caller(username="daniel"), "ada") is True
    assert await ua.owns_mind(db, ua.Caller(username="daniel"), "orphan") is True
    assert await ua.owns_mind(db, ua.Caller(username="sloan"), "ada") is False


async def test_a_listing_gives_the_primary_user_ownerless_rows_and_no_others(
    db, monkeypatch
):
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")
    caller = ua.Caller(username="daniel")
    keys = await ua.visible_minds(db, caller)
    known = await ua.all_mind_keys(db)
    rows = [
        {"id": "mine", "mind_id": DANIEL_MIND},
        {"id": "legacy", "mind_id": "ada"},
        {"id": "theirs", "mind_id": SLOAN_MIND},
    ]

    kept = ua.filter_sessions(rows, caller, keys, known)

    assert [r["id"] for r in kept] == ["mine", "legacy"]


async def test_a_mind_registered_in_mixed_case_resolves_to_its_owner(db):
    """`minds.name` is stored as registered and SQLite compares TEXT byte-wise,
    so a mind called "Hex" was listed by its owner and refused by every route
    beneath the listing."""
    await db.execute(
        "INSERT INTO minds (mind_id, name, owner) VALUES (?, ?, ?)",
        ("7c0de000-0000-4000-8000-0000000000cc", "Hex", "daniel"),
    )
    await db.commit()

    assert await ua.owner_of_mind(db, "Hex") == "daniel"
    assert await ua.owner_of_mind(db, "hex") == "daniel"
    keys = await ua.visible_minds(db, ua.Caller(username="daniel"))
    assert "hex" in keys


async def test_a_mind_row_with_an_empty_name_contributes_no_key(db):
    """An empty key would match every session whose mind_id is null or blank,
    handing them to whoever owned the nameless row."""
    await db.execute(
        "INSERT INTO minds (mind_id, name, owner) VALUES (?, ?, ?)",
        ("8d1ef000-0000-4000-8000-0000000000dd", "", "sloan"),
    )
    await db.commit()

    keys = await ua.visible_minds(db, ua.Caller(username="sloan"))

    assert "" not in keys
    assert ua.filter_sessions(
        [{"id": "x", "mind_id": None}], ua.Caller(username="sloan"), keys, keys
    ) == []


# --- a mount is not a websocket -------------------------------------------


def test_a_mounted_sub_application_is_not_reported_as_a_websocket():
    """A `Mount` has a path and no methods, exactly like a websocket route.
    Reported as one, a `StaticFiles` mount would have had its whole subtree
    classified admin-only."""
    from starlette.applications import Starlette
    from starlette.routing import Mount

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.router.routes.append(Mount("/static", app=Starlette()))

    assert ua.RouteTable().unlisted(app) == []
