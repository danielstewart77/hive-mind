"""Broker insert_message idempotency tests.

Covers the sms-gateway retry case: the Android client redelivers the same
webhook on any non-2xx, so two near-simultaneous POSTs to /sms/inbound must
collapse to a single broker row without a UNIQUE-constraint 500.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from comms import broker


def _run(coro):
    return asyncio.run(coro)


def test_insert_message_is_idempotent_on_duplicate_id() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = await broker.init_db(os.path.join(tmp, "broker.db"))
            try:
                first = await broker.insert_message(
                    db,
                    message_id="sms-+15551234567-236",
                    conversation_id="sms-+15551234567",
                    from_mind="sms-gateway",
                    to_mind="ada",
                    message_number=1,
                    content="hello",
                    rolling_summary="",
                    metadata={"source": "sms-inbound"},
                    status="pending",
                )
                assert first["existing"] is False

                second = await broker.insert_message(
                    db,
                    message_id="sms-+15551234567-236",
                    conversation_id="sms-+15551234567",
                    from_mind="sms-gateway",
                    to_mind="ada",
                    message_number=1,
                    content="hello",
                    rolling_summary="",
                    metadata={"source": "sms-inbound"},
                    status="pending",
                )
                assert second["existing"] is True
                assert second["id"] == first["id"]

                rows = await broker.get_messages(db, "sms-+15551234567")
                assert len(rows) == 1
            finally:
                await db.close()

    _run(scenario())


def test_insert_message_collapses_message_number_collision_silently() -> None:
    """Two duplicate webhook deliveries that race past get_next_message_number
    both compute the same message_number for the same conversation. With
    INSERT OR IGNORE on the PK, the second collapses to existing rather than
    raising on the (conversation_id, message_number) UNIQUE constraint.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = await broker.init_db(os.path.join(tmp, "broker.db"))
            try:
                await broker.insert_message(
                    db,
                    message_id="sms-+15551234567-236",
                    conversation_id="sms-+15551234567",
                    from_mind="sms-gateway",
                    to_mind="ada",
                    message_number=1,
                    content="hello",
                    rolling_summary="",
                    metadata=None,
                    status="pending",
                )
                # Duplicate webhook → same id, same conversation/number.
                # Pre-fix, the second INSERT raised IntegrityError on the
                # (conversation_id, message_number) UNIQUE constraint.
                dup = await broker.insert_message(
                    db,
                    message_id="sms-+15551234567-236",
                    conversation_id="sms-+15551234567",
                    from_mind="sms-gateway",
                    to_mind="ada",
                    message_number=1,
                    content="hello",
                    rolling_summary="",
                    metadata=None,
                    status="pending",
                )
                assert dup["existing"] is True
            finally:
                await db.close()

    _run(scenario())
