"""A harness reporting its own sub-agents, through the real route.

The route is the only place that can turn what a harness holds into what the
ledger stores, which is why it is driven here rather than reviewed: all
three harnesses send an id, none of them can say which *kind* of id it is,
and the record is worthless if the wrong kind is stored or the mind it is
filed under cannot be trusted.

What a write produced is read back on a plain driver connection, as every
suite against this database does: a layer that writes and reads the same
wrong column passes its own round trip.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from fastapi.testclient import TestClient

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

from comms import user_access as ua  # noqa: E402
from comms_broker_pg import (  # noqa: E402,F401 - fixtures
    broker_db,
    execute,
    fetch,
    put_credential_row,
    put_mind,
    scratch_dsn,
)

ADMIN_TOKEN = "admin-token-for-tests"  # secret-guard: allow
SHARED = "shared-token-for-tests"  # secret-guard: allow
SKIPPY_TOKEN = "skippy-mind-token-for-tests"  # secret-guard: allow
MORDECAI_TOKEN = "mordecai-mind-token-for-tests"  # secret-guard: allow

SKIPPY = "14cb820b-4a42-4f04-a593-54f532fd1d2f"
MORDECAI = "aa526b2b-0000-4000-8000-0000000000bb"

#: One conversation, named three ways. The gateway's own row id, the
#: conversation id every spawn is handed, and the thread id codex mints for
#: itself and reports back — and a hook holds exactly one of the three
#: depending on which harness it fires in.
SESSION_ID = "sess-0001"
CONVERSATION_ID = "conv-0001"
HARNESS_SID = "codex-thread-0001"


@pytest.fixture()
def gateway(broker_db, monkeypatch):
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SHARED)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("COMMS_PRIMARY_USER", "daniel")

    async def seed():
        for mind_id, name in ((SKIPPY, "skippy"), (MORDECAI, "mordecai")):
            await put_mind(mind_id, name, owner="daniel",
                           model="claude-opus-5", harness="claude",
                           gateway_url="http://127.0.0.1:8433")
        for holder, token in (("skippy", SKIPPY_TOKEN),
                              ("mordecai", MORDECAI_TOKEN)):
            await put_credential_row(holder, "daniel", ua.MIND,
                                     ua.digest(token))
        await execute(
            "INSERT INTO core.conversations (id, created_at) VALUES ($1, 0)",
            CONVERSATION_ID)
        await execute(
            'INSERT INTO core."sessions" (id, conversation_id, surface, '
            'surface_id, mind_id, created_at, last_active, status, '
            "harness_sid) VALUES ($1, $2, 'telegram', '111', $3, 0, 0, "
            "'idle', $4)",
            SESSION_ID, CONVERSATION_ID, SKIPPY, HARNESS_SID)

    asyncio.run(seed())

    from comms import server

    with TestClient(server.app) as client:
        yield client


def _report(client, token=SKIPPY_TOKEN, **body):  # secret-guard: allow
    payload = {"agent_id": "a1b2c3", "state": "spawned", "mind_id": SKIPPY}
    payload.update(body)
    return client.post("/staff/agents/events", json=payload,
                       headers={"Authorization": f"Bearer {token}"})


async def _rows():
    return await fetch(
        "SELECT id, mind_id, agent_type, about, session_id, "
        "conversation_ref, parent_agent_id, finished_at, outcome "
        "FROM staff.agents")


def test_a_reported_child_is_stored_under_the_conversation_it_belongs_to(
        gateway):
    """The conversation id every spawn is handed, which is what a claude
    hook holds."""
    answer = _report(gateway, conversation=CONVERSATION_ID,
                     kind="refute-tests", label="story 6.9.1")

    assert answer.status_code == 200, answer.text
    rows = asyncio.run(_rows())
    assert len(rows) == 1
    assert rows[0]["session_id"] == SESSION_ID
    assert rows[0]["conversation_ref"] == CONVERSATION_ID
    assert rows[0]["agent_type"] == "refute-tests"
    assert rows[0]["about"] == "story 6.9.1"
    assert rows[0]["mind_id"] == SKIPPY


def test_a_child_reported_against_the_gateways_own_session_id_resolves(
        gateway):
    """A codex or dsh spawn is handed this id outright."""
    assert _report(gateway, conversation=SESSION_ID).status_code == 200

    rows = asyncio.run(_rows())
    assert rows[0]["session_id"] == SESSION_ID


def test_a_child_reported_against_a_harness_thread_id_resolves(gateway):
    """All a codex hook knows is the thread id codex minted for itself, so a
    recorder that only understood conversation ids stored none of them."""
    assert _report(gateway, conversation=HARNESS_SID).status_code == 200

    rows = asyncio.run(_rows())
    assert rows[0]["session_id"] == SESSION_ID
    assert rows[0]["conversation_ref"] == HARNESS_SID


def test_a_child_belonging_to_no_hive_conversation_is_still_recorded(gateway):
    """An operator's own session at a terminal shares this host's hooks and
    this mind's environment. The child still ran, and the record of it says
    where it came from even though no page will draw it."""
    assert _report(gateway, conversation="not-a-conversation").status_code == 200

    rows = asyncio.run(_rows())
    assert rows[0]["session_id"] is None
    assert rows[0]["conversation_ref"] == "not-a-conversation"


def test_a_child_filed_under_another_mind_is_refused_and_nothing_is_written(
        gateway):
    """The class admits every mind's own credential and every surface bot's
    service value, so an unchecked mind field is one any holder could file a
    child under any mind."""
    answer = _report(gateway, token=MORDECAI_TOKEN,  # secret-guard: allow
                     conversation=CONVERSATION_ID)

    assert answer.status_code == 404
    assert asyncio.run(_rows()) == []


def test_an_ending_carries_its_outcome_and_closes_the_record(gateway):
    _report(gateway, conversation=CONVERSATION_ID)

    answer = _report(gateway, state="ended", outcome="error",
                     conversation=CONVERSATION_ID)

    assert answer.status_code == 200
    rows = asyncio.run(_rows())
    assert len(rows) == 1, "an ending is the same child, not a second one"
    assert rows[0]["finished_at"] is not None
    assert rows[0]["outcome"] == "error"


def test_a_child_names_the_child_that_spawned_it(gateway):
    _report(gateway, conversation=CONVERSATION_ID)

    _report(gateway, agent_id="d4e5f6", parent_agent_id="a1b2c3",
            conversation=CONVERSATION_ID)

    rows = {r["id"]: r for r in asyncio.run(_rows())}
    child = rows[f"{SKIPPY}:d4e5f6"]
    assert child["parent_agent_id"] == f"{SKIPPY}:a1b2c3"


def test_a_state_nobody_defined_is_refused_rather_than_stored(gateway):
    answer = _report(gateway, state="pondering", conversation=CONVERSATION_ID)

    assert answer.status_code == 400
    assert asyncio.run(_rows()) == []


def test_a_report_naming_no_agent_is_refused(gateway):
    """A hook whose payload parse failed sends an empty string, and a record
    with no identity is one every later event collides with."""
    answer = _report(gateway, agent_id="   ", conversation=CONVERSATION_ID)

    assert answer.status_code == 400
    assert asyncio.run(_rows()) == []
