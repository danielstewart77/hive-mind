# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
"""A correct guess never claims a bot past its own lockout.

The guess is read against the phrase on offer, and the claim lands only while
that phrase is still on offer. A phrase retired between the read and the
claim - by the wrong guess that reached the limit, or by a fresh mint -
claims nothing.
"""
from __future__ import annotations

import asyncio

import hive_data
import pytest

from comms import surface_claims
from comms_broker_pg import broker_db, put_mind, scratch_dsn  # noqa: F401 - fixtures


@pytest.fixture()
def db(broker_db):
    return broker_db


def test_a_right_guess_read_before_the_phrase_was_retired_claims_nothing(
        db, monkeypatch):
    real_row = surface_claims._row

    async def read_then_lockout(mind_id, key):
        row = await real_row(mind_id, key)
        # The eighth wrong guess lands between this read and the claim.
        await hive_data.broker_ops.update_claim(
            mind_id, key, phrase_hash=None, phrase_issued_at=None,
            phrase_expires_at=None, phrase_attempts=0, released_at=1050.0)
        return row

    async def go():
        await hive_data.connect()
        try:
            await put_mind("m1")
            phrase = await surface_claims.mint_phrase("m1", "telegram", now=1000.0)
            monkeypatch.setattr(surface_claims, "_row", read_then_lockout)
            outcome = await surface_claims.attempt_claim(
                "m1", "telegram", "8776938611", phrase, now=1060.0)
            monkeypatch.setattr(surface_claims, "_row", real_row)
            state = await surface_claims.claim_state("m1", "telegram", now=1060.0)
        finally:
            await hive_data.close(drain_timeout=5.0)
        return outcome, state

    outcome, state = asyncio.run(go())
    assert outcome.claimed is False
    assert state.claimed_ref in (None, "")
