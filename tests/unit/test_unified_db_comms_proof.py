"""The post-switch comms proof writes a turn and marks nothing that is live.

It used to stamp a marker into `core.minds.model` on a real mind — `NOT
NULL`, load-bearing, and read by every spawn — and hold it there for the
length of an HTTP call. These tests are about what it does instead, and the
first of them is the one that matters: the proof must not touch a live row
at all.

The gateway is stubbed at the transport, because a cutover check is a service
call and the question here is what the check does with the answer, not
whether a server is up. The connection records statements and answers the
one row it is given, so the test can say what landed in Postgres and what
did not.
"""

from __future__ import annotations

import asyncio

import pytest

from unified_db import postflight


class _Conn:
    """A connection that answers prepared rows and records every statement."""

    def __init__(self, message_row: dict | None, minds: list[str],
                 holds_the_turn: bool = False):
        self.message_row = message_row
        self.minds = minds
        #: Answer the turn the check actually posted, whatever it generated.
        #: Without this the "Postgres holds the turn" branch is unreachable
        #: and every test passes on the missing-row failure instead.
        self.holds_the_turn = holds_the_turn
        self.statements: list[str] = []
        self.posted_conversation: str | None = None

    async def fetchrow(self, sql: str, *args: object):
        self.statements.append(sql)
        if self.holds_the_turn:
            return {"conversation_id": self.posted_conversation,
                    "from_mind": "p", "to_mind": "p"}
        return self.message_row

    async def fetch(self, sql: str, *args: object):
        self.statements.append(sql)
        return [{"mind_id": m} for m in self.minds]

    async def execute(self, sql: str, *args: object) -> None:
        self.statements.append(sql)

    async def fetchval(self, sql: str, *args: object):
        self.statements.append(sql)
        return None


@pytest.fixture
def gateway(monkeypatch):
    """A gateway that accepts everything and lists the minds it is given."""
    posted: list[tuple[str, dict | None]] = []

    def _call(method, url, token=None, body=None, timeout=20.0):
        posted.append((url, body))
        if url.endswith("/broker/minds") and method == "GET":
            return {"minds": [{"id": m} for m in gateway.minds]}
        if url.endswith("/broker/messages"):
            return {"status": "dispatched",
                    "conversation_id": (body or {}).get("conversation_id"),
                    "message_id": "x"}
        return {}

    monkeypatch.setattr(postflight, "_call", _call)
    monkeypatch.setattr(postflight, "_reachable", lambda url: None)
    monkeypatch.setenv("COMMS_URL", "http://127.0.0.1:8426")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "t")
    gateway.posted = posted
    gateway.minds = []
    return gateway


def _turn_of(posted) -> dict:
    return next(body for url, body in posted
                if url.endswith("/broker/messages"))


def _conn_that_holds_the_turn(gateway, minds):
    """A connection that answers the turn the check posted, so the pass path
    is reachable and a failure after it can be told apart from the one
    before it."""
    conn = _Conn(None, minds, holds_the_turn=True)

    inner = postflight._call

    def recording(method, url, token=None, body=None, timeout=20.0):
        if url.endswith("/broker/messages") and body:
            conn.posted_conversation = body["conversation_id"]
        return inner(method, url, token, body, timeout)

    import unittest.mock
    patcher = unittest.mock.patch.object(postflight, "_call", recording)
    patcher.start()
    return conn, patcher


def test_the_proof_never_writes_to_a_live_minds_row(gateway):
    """The defect, stated as the thing that must not happen.

    A mind whose `model` is a name the proxy does not list has every spawn
    refused, and this check fires as the hive comes back up.
    """
    conn = _Conn({"conversation_id": None, "from_mind": "p", "to_mind": "p"},
                 ["postflight-x"])
    # The row it finds has to be the turn's own conversation for a pass; what
    # is asserted here is only what the check wrote.
    asyncio.run(postflight._comms(conn))

    marking = [s for s in conn.statements
               if "UPDATE" in s.upper() and "core.minds" in s]
    assert marking == [], marking


def test_a_turn_the_gateway_accepted_and_postgres_does_not_hold_is_a_failure(
        gateway):
    """The whole question: a gateway on SQLite writes it where this cannot see."""
    conn = _Conn(None, ["postflight-x"])

    state, detail = asyncio.run(postflight._comms(conn))

    assert state == "fail"
    assert "another store" in detail


def test_a_turn_found_under_the_wrong_conversation_is_a_failure(gateway):
    conn = _Conn({"conversation_id": "somebody-elses",
                  "from_mind": "p", "to_mind": "p"}, ["postflight-x"])

    state, detail = asyncio.run(postflight._comms(conn))

    assert state == "fail"
    assert "somebody-elses" in detail


def test_a_gateway_listing_minds_the_database_does_not_hold_is_a_failure(
        gateway):
    """A registry that disagrees with core.minds is a registry on another store.

    The turn is answered here, so this gets past the missing-row failure and
    actually reaches the registry comparison.
    """
    gateway.minds = ["a-mind-postgres-never-heard-of"]
    conn, patcher = _conn_that_holds_the_turn(gateway, ["one", "two"])
    try:
        state, detail = asyncio.run(postflight._comms(conn))
    finally:
        patcher.stop()

    assert state == "fail"
    assert "different minds than core.minds" in detail


def test_a_turn_in_postgres_and_a_matching_registry_passes(gateway):
    """The pass, so every failure above is a failure rather than the default."""
    gateway.minds = ["one", "two"]
    conn, patcher = _conn_that_holds_the_turn(gateway, ["one", "two"])
    try:
        state, detail = asyncio.run(postflight._comms(conn))
    finally:
        patcher.stop()

    assert state == "ok", detail
    assert "core.messages" in detail


def test_the_proof_posts_a_real_conversation_turn(gateway):
    conn = _Conn(None, [])
    asyncio.run(postflight._comms(conn))

    turn = _turn_of(gateway.posted)
    assert turn["from"] == turn["to"]
    assert turn["content"].startswith("postflight turn ")
    assert turn["conversation_id"].startswith("postflight-")


def test_the_proof_registers_its_own_mind_at_a_dead_port(gateway):
    """So the background wakeup fails fast instead of reaching a real mind."""
    conn = _Conn(None, [])
    asyncio.run(postflight._comms(conn))

    registration = next(body for url, body in gateway.posted
                        if url.endswith("/broker/minds") and body)
    assert registration["gateway_url"] == "http://127.0.0.1:1"
    assert registration["mind_id"].startswith("postflight-")


def test_the_proof_removes_its_own_rows_even_when_it_fails(gateway):
    """A check must not leave a conversation behind for a mind to pick up."""
    conn = _Conn(None, ["postflight-x"])

    asyncio.run(postflight._comms(conn))

    deletes = [s for s in conn.statements if s.startswith("DELETE")]
    assert any("core.messages" in s for s in deletes)
    assert any("core.conversations" in s for s in deletes)
    assert any("core.minds" in s for s in deletes)
    assert any("core.sessions" in s for s in deletes)


def test_the_proof_removes_its_own_rows_when_the_gateway_raises(
        gateway, monkeypatch):
    def explodes(method, url, token=None, body=None, timeout=20.0):
        if url.endswith("/broker/messages"):
            raise OSError("the gateway hung up")
        return {}

    monkeypatch.setattr(postflight, "_call", explodes)
    conn = _Conn(None, [])

    with pytest.raises(OSError):
        asyncio.run(postflight._comms(conn))

    assert any("DELETE" in s and "core.minds" in s for s in conn.statements)
