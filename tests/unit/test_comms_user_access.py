"""Who is asking comms, and which conversations answer them.

A conversation's owner is derived — session names a mind, `broker.minds` names
the mind's owner — and the gate in front of every session route is the thing
that has to agree with that derivation on every path, websockets included.
"""

from __future__ import annotations

import os
import sys

import asyncio

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

# Each test file needing the gateway package puts `nervous-system` on the
# path itself; it holds a hyphen, so it can never be a package name.
NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

from comms import user_access as ua
from comms_broker_pg import (  # noqa: F401 - fixtures
    broker_db,
    broker_store,
    fetch,
    put_credential_row,
    put_mind,
)


# ---------------------------------------------------------------------------
# Fixtures: two users' minds in a scratch Postgres
# ---------------------------------------------------------------------------

# Invented values, never real credentials. Named once so the guard has one
# line to look at and the tests read as behaviour rather than as strings.
SHARED = "shared-token-for-tests"  # secret-guard: allow
ADMIN_TOKEN = "admin-token-for-tests"  # secret-guard: allow
SLOAN_TOKEN = "sloan-token-for-tests"  # secret-guard: allow
# A mind's own credential. The machinery routes answer to this rather than
# to the one shared value every mind used to hold.
SKIPPY_OWN = "skippy-mind-value-for-tests"  # secret-guard: allow
UNISSUED = "never-issued-for-tests"  # secret-guard: allow

DANIEL_MIND = "14cb820b-4a42-4f04-a593-54f532fd1d2f"
SLOAN_MIND = "9f3c1a77-0000-4000-8000-0000000000aa"


async def _seed_minds() -> None:
    await put_mind(DANIEL_MIND, "skippy", owner="daniel")
    await put_mind(SLOAN_MIND, "arnold", owner="sloan")
    await put_mind("3b0e0000-0000-4000-8000-0000000000bb", "orphan")


@pytest.fixture()
async def db(broker_store):
    """Two users' minds and one nobody owns, in a database of this test's own."""
    await _seed_minds()


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
# The credential belongs to a holder, the authority to its owner
# ---------------------------------------------------------------------------


async def test_a_minted_credential_verifies_and_an_unminted_value_does_not(db):
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN, role=ua.MIND)

    assert await ua.user_for_token(SLOAN_TOKEN) == "sloan"
    assert await ua.user_for_token(UNISSUED) is None


async def test_a_credential_is_stored_as_a_digest_and_never_in_the_clear(db):
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN, role=ua.MIND)

    stored = [r["token_sha"] for r in await fetch("SELECT token_sha FROM core.credentials")]

    assert stored == [ua.digest(SLOAN_TOKEN)]
    assert SLOAN_TOKEN not in stored


async def test_two_holders_of_one_person_each_get_their_own_value(db):
    """The whole point of keying on the holder. With the conflict target left
    on the username, issuing the second holder overwrites the first and the
    first value stops resolving — so both values are asserted, not just the
    one minted last."""
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN, role=ua.MIND)
    await ua.put_credential("zach", "sloan", "zach-value", role=ua.MIND)

    assert await ua.user_for_token(SLOAN_TOKEN) == "sloan"
    assert await ua.user_for_token("zach-value") == "sloan"

    rows = await fetch("SELECT holder FROM core.credentials ORDER BY holder")
    assert [r["holder"] for r in rows] == ["arnold", "zach"]


async def test_the_resolved_credential_names_the_holder_that_presented_it(db):
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN, role=ua.MIND)

    found = await ua.credential_for_token(SLOAN_TOKEN)

    assert found.holder == "arnold"
    assert found.username == "sloan"
    assert found.role == ua.MIND


async def test_a_value_another_holder_already_holds_is_refused(db):
    """Two holders sharing one value is the resolver returning whichever row
    SQLite yields first — the second holder silently authenticating as the
    first one's owner."""
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN, role=ua.MIND)

    with pytest.raises(ValueError) as caught:
        await ua.put_credential("zach", "aiden", SLOAN_TOKEN, role=ua.MIND)

    assert "arnold" in str(caught.value)
    assert await ua.user_for_token(SLOAN_TOKEN) == "sloan"


async def test_a_credential_naming_no_holder_or_no_user_is_refused(db):
    with pytest.raises(ValueError):
        await ua.put_credential("", "sloan", SLOAN_TOKEN)
    with pytest.raises(ValueError):
        await ua.put_credential("arnold", "", SLOAN_TOKEN)
    with pytest.raises(ValueError):
        await ua.put_credential("arnold", "sloan", "")


async def test_an_unknown_role_is_refused(db):
    with pytest.raises(ValueError):
        await ua.put_credential(
            "arnold", "sloan", SLOAN_TOKEN, role="superuser"
        )


async def test_a_holder_naming_a_registered_mind_defaults_to_the_mind_role(db):
    """Unstated, the role is derived from the `minds` table rather than
    declared twice, so a mind that is deleted stops being machinery."""
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN)
    await ua.put_credential("a-laptop", "sloan", "laptop-value")

    minted = await ua.credential_for_token(SLOAN_TOKEN)
    other = await ua.credential_for_token("laptop-value")

    assert minted.role == ua.MIND
    assert other.role == ua.PERSON


# ---------------------------------------------------------------------------
# Revocation lands on the next request, for one holder only
# ---------------------------------------------------------------------------


async def test_revoking_a_credential_stops_it_verifying_in_the_same_process(db):
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN, role=ua.MIND)
    assert await ua.user_for_token(SLOAN_TOKEN) == "sloan"

    assert await ua.revoke_credential("arnold") is True

    # Same connection, same process, no re-import: a cache anywhere in the
    # read path would leave this returning "sloan".
    assert await ua.user_for_token(SLOAN_TOKEN) is None


async def test_revoking_one_holder_leaves_another_of_the_same_person_alone(db):
    """Revoking by user instead of by holder would take both down, which is
    the failure this whole change exists to prevent: the value on a box in
    somebody's bedroom must be retirable without retiring the one running
    here."""
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN, role=ua.MIND)
    await ua.put_credential("zach", "sloan", "zach-value", role=ua.MIND)

    assert await ua.revoke_credential("arnold") is True

    assert await ua.user_for_token(SLOAN_TOKEN) is None
    assert await ua.user_for_token("zach-value") == "sloan"


async def test_revoking_a_credential_nobody_minted_reports_that_it_did_nothing(db):
    assert await ua.revoke_credential("nobody") is False


async def test_reissuing_a_credential_clears_an_earlier_revocation(db):
    await ua.put_credential("arnold", "sloan", "first-value", role=ua.MIND)
    await ua.revoke_credential("arnold")

    await ua.put_credential("arnold", "sloan", "second-value", role=ua.MIND)

    assert await ua.user_for_token("second-value") == "sloan"
    assert await ua.user_for_token("first-value") is None


# ---------------------------------------------------------------------------
# What a presented value resolves to
# ---------------------------------------------------------------------------


async def test_the_shared_service_token_resolves_nobody(db, monkeypatch):
    """It was one value held by every mind, every surface bot and two boxes
    across the LAN, which made its theft indistinguishable from its use. It is
    not a credential any more, and nothing about the environment brings it
    back."""
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")

    caller = await ua.resolve_caller(SHARED)

    assert caller.recognised is False
    assert caller.is_machine is False


async def test_no_service_token_configured_does_not_open_the_gateway(db, monkeypatch):
    """An unset service token once meant "no gating", which served every
    route to every caller. Unsetting it is also the obvious way an operator
    retires it."""
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)

    for presented in (SHARED, UNISSUED, ""):
        caller = await ua.resolve_caller(presented)
        assert caller.recognised is False
        assert caller.sees_everything() is False


async def test_the_admin_token_reaches_everything(db, monkeypatch):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)

    caller = await ua.resolve_caller(ADMIN_TOKEN)

    assert caller.is_admin is True
    assert caller.sees_everything() is True


async def test_an_unknown_value_is_not_recognised(db):
    caller = await ua.resolve_caller(UNISSUED)

    assert caller.recognised is False


async def test_a_holder_credential_is_resolved_to_the_user_it_answers_for(db):
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN, role=ua.MIND)

    caller = await ua.resolve_caller(SLOAN_TOKEN)

    assert caller.username == "sloan"
    assert caller.holder == "arnold"
    assert caller.sees_everything() is False


async def test_a_minds_credential_is_machinery_and_a_persons_is_not(db):
    """The machinery routes are addressed by client reference rather than by
    conversation, so there is nothing for an owner check to scope — a
    credential admitted there reaches every conversation through the back of
    the house. A mind's own credential has to reach them or no rotation hook
    on any mind can report a turn; a person's must not."""
    await ua.put_credential("arnold", "sloan", SLOAN_TOKEN, role=ua.MIND)
    await ua.put_credential("hive-tools", "daniel", "tools-value", role=ua.SERVICE)
    await ua.put_credential("sloan-laptop", "sloan", "person-value", role=ua.PERSON)

    assert (await ua.resolve_caller(SLOAN_TOKEN)).is_machine is True
    assert (await ua.resolve_caller("tools-value")).is_machine is True
    assert (await ua.resolve_caller("person-value")).is_machine is False


# ---------------------------------------------------------------------------
# Ownership derivation
# ---------------------------------------------------------------------------


async def test_a_mind_resolves_to_its_owner_by_uuid_and_by_name(db):
    assert await ua.owner_of_mind(DANIEL_MIND) == "daniel"
    assert await ua.owner_of_mind("skippy") == "daniel"
    assert await ua.owner_of_mind(SLOAN_MIND) == "sloan"


async def test_a_mind_with_no_owner_and_a_mind_that_is_not_registered_resolve_to_none(
    db,
):
    assert await ua.owner_of_mind("orphan") is None
    assert await ua.owner_of_mind("no-such-mind") is None


async def test_the_keys_a_caller_owns_cover_both_the_uuid_and_the_name(db):
    keys = await ua.visible_minds(ua.Caller(username="daniel"))

    assert DANIEL_MIND in keys
    assert "skippy" in keys
    assert SLOAN_MIND not in keys
    assert "arnold" not in keys


async def test_a_listing_keeps_only_the_callers_own_conversations(db):
    keys = await ua.visible_minds(ua.Caller(username="sloan"))
    rows = [
        {"id": "a", "mind_id": DANIEL_MIND},
        {"id": "b", "mind_id": SLOAN_MIND},
        {"id": "c", "mind_id": "skippy"},
    ]

    kept = ua.filter_sessions(rows, ua.Caller(username="sloan"), keys)

    assert [r["id"] for r in kept] == ["b"]


async def test_an_admin_listing_keeps_every_conversation(db):
    caller = ua.Caller(is_admin=True, unscoped=True)
    keys = await ua.visible_minds(caller)
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

    import hive_data

    @asynccontextmanager
    async def lifespan(_app):
        # Opened on the loop the app runs on: asyncpg ties a pool to the loop
        # that opened it. With no database named, nothing is opened, which is
        # the window between process start and the pool being up.
        if broker_path is None:
            yield
            return
        await hive_data.connect()
        try:
            yield
        finally:
            await hive_data.close(drain_timeout=5.0)

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
        session_mind=session_mind,
    )
    return app, table


@pytest.fixture()
def broker_path(broker_db):
    """The scratch database, seeded, for an app that opens its own pool."""

    async def seed():
        await put_mind(DANIEL_MIND, "skippy", owner="daniel")
        await put_mind(SLOAN_MIND, "arnold", owner="sloan")
        # Constructing an input. The digest still comes from the module, so a
        # change to how a credential is stored fails these tests rather than
        # passing against a hash this file computed its own way.
        # Sloan himself on a surface — a person, so never machinery.
        await put_credential_row("sloan-phone", "sloan", ua.PERSON, ua.digest(SLOAN_TOKEN))
        # One of Daniel's minds, with its own value.
        await put_credential_row("skippy", "daniel", ua.MIND, ua.digest(SKIPPY_OWN))

    asyncio.run(seed())
    return broker_db


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


def test_a_minds_own_credential_reaches_its_owners_conversation(gated):
    answer = _get(gated, "/sessions/d-session", token=SKIPPY_OWN)

    assert answer.status_code == 200


def test_a_minds_own_credential_is_refused_another_users_conversation(gated):
    answer = _get(gated, "/sessions/s-session", token=SKIPPY_OWN)

    assert answer.status_code == 404


def test_the_shared_token_is_refused_through_the_gate(gated):
    """It was one value held by every mind, every surface bot and two boxes
    across the LAN. Nothing in the environment brings it back, and no flag
    softens it: a fallback nobody can observe is one that never gets
    removed."""
    assert _get(gated, "/sessions/d-session", token=SHARED).status_code == 401
    assert _get(gated, "/sessions/s-session", token=SHARED).status_code == 401
    assert _get(gated, "/sessions", token=SHARED).status_code == 401


def test_an_unconfigured_gateway_does_not_serve_open(broker_path, monkeypatch):
    """An unset service token once meant "no gating", which served every route
    to every caller — and unsetting it is the obvious way an operator retires
    it. A gateway that cannot establish who is calling refuses, while the
    holder table still decides for everyone holding a real credential."""
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("COMMS_PRIMARY_USER", raising=False)
    app, _ = _gated_app(
        broker_path, {"d-session": DANIEL_MIND, "s-session": SLOAN_MIND}
    )

    assert _get(app, "/sessions/s-session", token=SHARED).status_code == 401
    assert _get(app, "/sessions/d-session").status_code == 401
    assert _get(app, "/sessions/s-session", token=SLOAN_TOKEN).status_code == 200


def test_the_admin_token_reaches_every_users_conversation(gated):
    assert _get(gated, "/sessions/d-session", token=ADMIN_TOKEN).status_code == 200
    assert _get(gated, "/sessions/s-session", token=ADMIN_TOKEN).status_code == 200


def test_an_admin_only_route_refuses_a_user_credential(gated):
    assert _get(gated, "/sessions/live", token=SLOAN_TOKEN).status_code == 401
    assert _get(gated, "/sessions/live", token=ADMIN_TOKEN).status_code == 200


def test_the_machinery_routes_serve_a_minds_own_credential(gated):
    """Every rotation hook on every mind reaches these, and the one shared
    value they all presented is gone. Refusing a mind's own credential here
    stops every per-turn report on the hive."""
    with TestClient(gated, raise_server_exceptions=False) as client:
        served = client.post(
            "/sessions/context",
            headers={"Authorization": "Bearer " + SKIPPY_OWN},
        )
        refused = client.post(
            "/sessions/context", headers={"Authorization": "Bearer " + SHARED}
        )

    assert served.status_code == 200
    assert refused.status_code == 401


def test_a_revoked_credential_is_refused_by_the_gate_on_the_very_next_request(
    gated, broker_path
):
    """The gate consults the table per request. A cache anywhere in that read
    path leaves the second call answering 200 with nothing restarted."""
    with TestClient(gated, raise_server_exceptions=False) as client:
        headers = {"Authorization": "Bearer " + SLOAN_TOKEN}
        before = client.get("/sessions/s-session", headers=headers)
        # On the app's own loop, where its pool lives.
        client.portal.call(ua.revoke_credential, "sloan-phone")
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
    """Between process start and the pool opening there is a window. Passing a session route through it unresolved is the one thing
    this gate must never do."""
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    app, _ = _gated_app(None, {})

    assert _get(app, "/sessions", token=SHARED).status_code == 503


# --- the credential classes are not interchangeable -----------------------


async def test_a_persons_credential_is_recognised_but_is_not_machinery(db):
    await ua.put_credential(
        "sloan-phone", "sloan", SLOAN_TOKEN, role=ua.PERSON
    )

    caller = await ua.resolve_caller(SLOAN_TOKEN)

    assert caller.recognised is True
    assert caller.is_machine is False


# --- a conversation whose mind has no owner -------------------------------


async def test_an_ownerless_conversation_answers_to_the_primary_user(
    db, monkeypatch
):
    """`mind_id` carried a `DEFAULT 'ada'` for most of this schema's life, and
    a mind row's `owner` is nullable. Refusing those to everyone would lose
    the operator their own history the moment scoping is switched on."""
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")

    assert await ua.owns_mind(ua.Caller(username="daniel"), "ada") is True
    assert await ua.owns_mind(ua.Caller(username="daniel"), "orphan") is True
    assert await ua.owns_mind(ua.Caller(username="sloan"), "ada") is False


async def test_a_listing_gives_the_primary_user_ownerless_rows_and_no_others(
    db, monkeypatch
):
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")
    caller = ua.Caller(username="daniel")
    keys = await ua.visible_minds(caller)
    known = await ua.all_mind_keys()
    rows = [
        {"id": "mine", "mind_id": DANIEL_MIND},
        {"id": "legacy", "mind_id": "ada"},
        {"id": "theirs", "mind_id": SLOAN_MIND},
    ]

    kept = ua.filter_sessions(rows, caller, keys, known)

    assert [r["id"] for r in kept] == ["mine", "legacy"]


async def test_a_mind_registered_in_mixed_case_resolves_to_its_owner(db):
    """`minds.name` is stored as registered and compared byte-wise, so a mind
    called "Hex" was listed by its owner and refused by every route beneath
    the listing."""
    await put_mind("7c0de000-0000-4000-8000-0000000000cc", "Hex", owner="daniel")

    assert await ua.owner_of_mind("Hex") == "daniel"
    assert await ua.owner_of_mind("hex") == "daniel"
    keys = await ua.visible_minds(ua.Caller(username="daniel"))
    assert "hex" in keys


async def test_a_mind_row_with_an_empty_name_contributes_no_key(db):
    """An empty key would match every session whose mind_id is null or blank,
    handing them to whoever owned the nameless row."""
    await put_mind("8d1ef000-0000-4000-8000-0000000000dd", "", owner="sloan")

    keys = await ua.visible_minds(ua.Caller(username="sloan"))

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

# ---------------------------------------------------------------------------
# Values as they arrive in practice
# ---------------------------------------------------------------------------


async def test_a_value_carrying_a_non_ascii_character_still_resolves(db):
    """A value handed over in person gets pasted through a document, which
    turns a hyphen into a non-breaking one without showing it. Comparing the
    digests as `str` raises TypeError on anything outside ASCII, and on a
    websocket handshake that 500 reaches the gateway as "this mind has no
    terminal route"."""
    smart = "arnolds\u2011own\u2011value"
    await ua.put_credential("arnold", "sloan", smart, role=ua.MIND)

    assert await ua.user_for_token(smart) == "sloan"
    assert await ua.user_for_token("arnolds-own-value") is None


async def test_the_shared_provenance_is_not_a_holder(db):
    """Lucent files hive-wide rules under it. Minting it here would let the
    two services disagree about whether it names a party."""
    with pytest.raises(ValueError):
        await ua.put_credential("shared", "daniel", "a-value")


def test_a_database_that_does_not_answer_is_503_not_500_or_401(
        broker_path, monkeypatch):
    """Postgres restarting is neither a bad credential nor a server bug.
    SQLite almost never raised here; a pool can, and every authenticated
    request and websocket handshake answered 500 while it did."""
    import hive_data

    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    app, _ = _gated_app(broker_path, {})
    real_resolve = ua.resolve_caller

    async def store_down(_bearer):
        raise hive_data.errors.Unreachable("connection refused")

    monkeypatch.setattr(ua, "resolve_caller", store_down)
    assert _get(app, "/sessions", token=SLOAN_TOKEN).status_code == 503
    monkeypatch.setattr(ua, "resolve_caller", real_resolve)


def test_a_conversation_lookup_that_times_out_is_503_not_404(
        broker_path, monkeypatch):
    """A failed read is not an absent row: a 404 tells the user their
    conversation does not exist because a lock timed out."""
    import hive_data

    app, _ = _gated_app(broker_path, {})
    for layer in app.user_middleware:
        if layer.cls is ua.UserAccess:
            async def timed_out(_session_id):
                raise hive_data.errors.TimedOut("statement deadline")
            layer.kwargs["session_mind"] = timed_out

    assert _get(app, "/sessions/s-session", token=SLOAN_TOKEN).status_code == 503
