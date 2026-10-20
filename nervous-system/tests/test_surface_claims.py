"""A bot learns the one person it answers, and cannot be talked out of it.

The claim is the whole of a bot's authorization now, so every one of these
drives `comms.surface_claims` itself rather than a fixture standing in for
it: the window is asserted from inside and outside with an injected clock,
the race is run concurrently against the real conditional update, and the
survival case reopens the database rather than trusting the object.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import aiosqlite
import pytest

from comms import surface_claims


async def _open(path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await surface_claims.init(db)
    return db


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db_path():
    with tempfile.TemporaryDirectory() as root:
        yield os.path.join(root, "broker.db")


def test_the_phrase_claims_the_bot_for_whoever_sent_it(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            outcome = await surface_claims.attempt_claim(
                db, "m1", "telegram", "8776938611", phrase, now=1060.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1060.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is True
    assert outcome.reason == "claimed"
    assert state.claimed_ref == "8776938611"
    assert state.phrase_pending is False


def test_a_claim_is_still_there_when_the_database_is_reopened(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(
                db, "m1", "telegram", "777", phrase, now=1001.0
            )
        finally:
            await db.close()
        again = await _open(db_path)
        try:
            return await surface_claims.claim_state(again, "m1", "telegram", now=2000.0)
        finally:
            await again.close()

    assert _run(go()).claimed_ref == "777"


def test_a_message_that_is_not_the_phrase_leaves_the_bot_unclaimed(db_path):
    async def go():
        db = await _open(db_path)
        try:
            await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            outcome = await surface_claims.attempt_claim(
                db, "m1", "telegram", "777", "let me in", now=1001.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1001.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is False
    assert outcome.reason == "mismatch"
    assert state.claimed is False


def test_a_second_account_sending_the_phrase_afterwards_is_refused(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(db, "m1", "telegram", "111", phrase, now=1001.0)
            outcome = await surface_claims.attempt_claim(
                db, "m1", "telegram", "222", phrase, now=1002.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1002.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is False
    assert state.claimed_ref == "111"


def test_the_phrase_still_claims_just_inside_the_window(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            return await surface_claims.attempt_claim(
                db, "m1", "telegram", "777", phrase, now=1000.0 + 29 * 60
            )
        finally:
            await db.close()

    assert _run(go()).claimed is True


def test_the_phrase_stops_claiming_once_thirty_minutes_have_passed(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            outcome = await surface_claims.attempt_claim(
                db, "m1", "telegram", "777", phrase, now=1000.0 + 30 * 60 + 1
            )
            state = await surface_claims.claim_state(
                db, "m1", "telegram", now=1000.0 + 30 * 60 + 1
            )
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is False
    assert outcome.reason == "expired"
    assert state.claimed is False
    assert state.phrase_pending is False


def test_minting_again_retires_the_previous_phrase_and_the_new_one_works(db_path):
    async def go():
        db = await _open(db_path)
        try:
            first = await surface_claims.mint_phrase(
                db, "m1", "telegram", now=1000.0, phrase="anchor basalt cinder"
            )
            second = await surface_claims.mint_phrase(
                db, "m1", "telegram", now=1100.0, phrase="willow zephyr umber"
            )
            stale = await surface_claims.attempt_claim(
                db, "m1", "telegram", "777", first, now=1200.0
            )
            fresh = await surface_claims.attempt_claim(
                db, "m1", "telegram", "777", second, now=1201.0
            )
            return stale, fresh
        finally:
            await db.close()

    stale, fresh = _run(go())
    assert stale.claimed is False
    assert stale.reason == "mismatch"
    assert fresh.claimed is True


def test_a_cleared_bot_can_be_claimed_by_somebody_else(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(db, "m1", "telegram", "111", phrase, now=1001.0)
            cleared = await surface_claims.clear_claim(db, "m1", "telegram")
            second = await surface_claims.mint_phrase(db, "m1", "telegram", now=1100.0)
            outcome = await surface_claims.attempt_claim(
                db, "m1", "telegram", "222", second, now=1101.0
            )
            return cleared, outcome
        finally:
            await db.close()

    cleared, outcome = _run(go())
    assert cleared.claimed is False
    assert outcome.claimed is True
    assert outcome.claimed_ref == "222"


def test_a_claimed_bot_refuses_to_put_a_phrase_back_on_offer(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(db, "m1", "telegram", "111", phrase, now=1001.0)
            with pytest.raises(ValueError):
                await surface_claims.mint_phrase(db, "m1", "telegram", now=1100.0)
            return await surface_claims.claim_state(db, "m1", "telegram", now=1100.0)
        finally:
            await db.close()

    assert _run(go()).claimed_ref == "111"


def test_two_people_sending_the_phrase_at_once_produce_one_claim(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            outcomes = await asyncio.gather(
                surface_claims.attempt_claim(db, "m1", "telegram", "111", phrase, now=1001.0),
                surface_claims.attempt_claim(db, "m1", "telegram", "222", phrase, now=1001.0),
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1002.0)
            return outcomes, state
        finally:
            await db.close()

    outcomes, state = _run(go())
    assert sum(1 for o in outcomes if o.claimed) == 1
    assert state.claimed_ref in {"111", "222"}


def test_a_phrase_typed_by_a_phone_keyboard_still_claims(db_path):
    async def go():
        db = await _open(db_path)
        try:
            await surface_claims.mint_phrase(
                db, "m1", "telegram", now=1000.0, phrase="anchor basalt cinder"
            )
            return await surface_claims.attempt_claim(
                db, "m1", "telegram", "777", "  Anchor  Basalt Cinder.  ", now=1001.0
            )
        finally:
            await db.close()

    outcome = _run(go())
    assert outcome.claimed is True
    assert outcome.claimed_ref == "777"


def test_an_installs_single_allow_list_entry_becomes_its_claim(db_path):
    async def go():
        db = await _open(db_path)
        try:
            outcome = await surface_claims.adopt_claim(
                db, "m1", "telegram", "8776938611", now=1000.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1000.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is True
    assert outcome.reason == "adopted"
    assert state.claimed_ref == "8776938611"


def test_adoption_never_moves_a_bot_that_is_already_claimed(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(db, "m1", "telegram", "111", phrase, now=1001.0)
            outcome = await surface_claims.adopt_claim(
                db, "m1", "telegram", "222", now=1002.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1002.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is False
    assert state.claimed_ref == "111"


def test_a_claim_on_one_surface_leaves_the_other_surface_alone(db_path):
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(db, "m1", "telegram", "111", phrase, now=1001.0)
            discord = await surface_claims.claim_state(db, "m1", "discord", now=1001.0)
            index = await surface_claims.claimed_index(db)
            return discord, index
        finally:
            await db.close()

    discord, index = _run(go())
    assert discord.claimed is False
    assert index["telegram"] == {"m1": "111"}
    assert index["discord"] == {}
