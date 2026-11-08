"""Broker insert_message idempotency tests.

Covers the sms-gateway retry case: the Android client redelivers the same
webhook on any non-2xx, so two near-simultaneous POSTs to /sms/inbound must
collapse to a single broker row without a uniqueness 500 — and the other side
of it: two *different* messages landing at once must both be kept, each with
its own number.
"""
from __future__ import annotations

import asyncio

import pytest

from comms import broker
from comms_broker_pg import broker_store, fetch, scratch_dsn  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio

CONVERSATION = "sms-+15551234567"


def _message(message_id: str, **overrides) -> dict:
    fields = {
        "message_id": message_id,
        "conversation_id": CONVERSATION,
        "from_mind": "sms-gateway",
        "to_mind": "ada",
        "content": "hello",
        "rolling_summary": "",
        "metadata": {"source": "sms-inbound"},
        "status": "pending",
    }
    fields.update(overrides)
    return fields


async def test_insert_message_is_idempotent_on_duplicate_id(broker_store) -> None:
    first = await broker.insert_message(**_message("sms-+15551234567-236"))
    second = await broker.insert_message(**_message("sms-+15551234567-236"))

    assert first["existing"] is False
    assert second["existing"] is True
    assert second["id"] == first["id"]
    rows = await fetch(
        "SELECT id FROM core.messages WHERE conversation_id = $1", CONVERSATION
    )
    assert len(rows) == 1


async def test_a_duplicate_racing_the_original_collapses_rather_than_raising(
    broker_store,
) -> None:
    """Two deliveries of one webhook at the same instant. Both reach the
    insert; one lands and the other is told it already exists."""
    outcomes = await asyncio.gather(
        broker.insert_message(**_message("sms-+15551234567-236")),
        broker.insert_message(**_message("sms-+15551234567-236")),
    )

    assert sorted(o["existing"] for o in outcomes) == [False, True]


async def test_two_different_messages_at_once_each_get_their_own_number(
    broker_store,
) -> None:
    """The number is decided inside the insert while the conversation is
    held. Read beforehand, both derived the same one and the second collided
    on (conversation_id, message_number) — dropped in silence under SQLite's
    `INSERT OR IGNORE`, refused outright by Postgres."""
    outcomes = await asyncio.gather(*(
        broker.insert_message(**_message(f"sms-+15551234567-{n}"))
        for n in range(5)
    ))

    assert all(o["existing"] is False for o in outcomes)
    rows = await fetch(
        "SELECT message_number FROM core.messages WHERE conversation_id = $1 "
        "ORDER BY message_number", CONVERSATION
    )
    assert [r["message_number"] for r in rows] == [1, 2, 3, 4, 5]
