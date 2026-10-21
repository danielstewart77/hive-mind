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


def test_a_phrase_is_retired_after_eight_wrong_guesses(db_path):
    """The route is reachable by anyone who can message an unclaimed bot, so
    a window with no counter is a window somebody can walk through."""
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(
                db, "m1", "telegram", now=1000.0, phrase="anchor basalt cinder"
            )
            for attempt in range(surface_claims.MAX_PHRASE_ATTEMPTS - 1):
                outcome = await surface_claims.attempt_claim(
                    db, "m1", "telegram", "666", f"guess {attempt}", now=1001.0
                )
                assert outcome.reason == "mismatch", attempt
            last = await surface_claims.attempt_claim(
                db, "m1", "telegram", "666", "one more", now=1002.0
            )
            # The real phrase no longer works either: the door is shut, not
            # merely shut to that guesser.
            after = await surface_claims.attempt_claim(
                db, "m1", "telegram", "777", phrase, now=1003.0
            )
            return last, after
        finally:
            await db.close()

    last, after = _run(go())
    assert last.reason == "too_many_attempts"
    assert after.claimed is False
    assert after.reason == "no_phrase"


def test_a_fresh_phrase_resets_the_guess_count(db_path):
    async def go():
        db = await _open(db_path)
        try:
            await surface_claims.mint_phrase(
                db, "m1", "telegram", now=1000.0, phrase="anchor basalt cinder"
            )
            for attempt in range(surface_claims.MAX_PHRASE_ATTEMPTS - 1):
                await surface_claims.attempt_claim(
                    db, "m1", "telegram", "666", f"guess {attempt}", now=1001.0
                )
            second = await surface_claims.mint_phrase(
                db, "m1", "telegram", now=1100.0, phrase="willow zephyr umber"
            )
            wrong = await surface_claims.attempt_claim(
                db, "m1", "telegram", "666", "still wrong", now=1101.0
            )
            right = await surface_claims.attempt_claim(
                db, "m1", "telegram", "777", second, now=1102.0
            )
            return wrong, right
        finally:
            await db.close()

    wrong, right = _run(go())
    assert wrong.reason == "mismatch"
    assert right.claimed is True


def test_adoption_never_takes_a_bot_back_after_it_was_released(db_path):
    """Adoption runs on every boot. A release that a restart undid would be
    the YAML deciding again, one reboot later — and on a logon-triggered
    Windows mind the boot is the very event that lets the new person type."""
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(db, "m1", "telegram", "111", phrase, now=1001.0)
            await surface_claims.clear_claim(db, "m1", "telegram")
            outcome = await surface_claims.adopt_claim(
                db, "m1", "telegram", "111", now=1002.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1002.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is False
    assert outcome.reason == "already_decided"
    assert state.claimed is False


def test_a_new_bot_on_the_same_mind_is_not_the_claimed_bot(db_path):
    """The claim was a claim on a bot. A different bot answering the person
    who claimed the old one has never been claimed at all."""
    async def go():
        db = await _open(db_path)
        try:
            await surface_claims.record_bot(db, "m1", "telegram", "old_bot", now=900.0)
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(db, "m1", "telegram", "111", phrase, now=1001.0)
            same = await surface_claims.record_bot(
                db, "m1", "telegram", "old_bot", now=1002.0
            )
            replaced = await surface_claims.record_bot(
                db, "m1", "telegram", "new_bot", now=1003.0
            )
            return same, replaced
        finally:
            await db.close()

    same, replaced = _run(go())
    assert same.claimed_ref == "111"
    assert replaced.claimed is False


def test_a_claimed_bot_never_has_a_phrase_on_offer(db_path):
    """Visible on the page as claimed, so a live phrase behind it could not
    be seen at all."""
    async def go():
        db = await _open(db_path)
        try:
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(db, "m1", "telegram", "111", phrase, now=1001.0)
            try:
                await surface_claims.mint_phrase(db, "m1", "telegram", now=1002.0)
            except ValueError:
                pass
            return await surface_claims.claim_state(db, "m1", "telegram", now=1003.0)
        finally:
            await db.close()

    state = _run(go())
    assert state.claimed_ref == "111"
    assert state.phrase_pending is False


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
            return await surface_claims.claim_state(db, "m1", "discord", now=1001.0)
        finally:
            await db.close()

    assert _run(go()).claimed is False


def test_a_registered_bot_that_was_never_claimed_still_adopts(db_path):
    """Saving a token on the bots page writes a row holding only the bot
    account. Treating that row as a decision made the documented way to set
    a bot up also the way to make it permanently unadoptable."""
    async def go():
        db = await _open(db_path)
        try:
            await surface_claims.record_bot(
                db, "m1", "telegram", "hive_m1_bot", now=1000.0
            )
            outcome = await surface_claims.adopt_claim(
                db, "m1", "telegram", "8776938611", now=1001.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1001.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is True
    assert outcome.reason == "adopted"
    assert state.claimed_ref == "8776938611"


def test_replacing_the_token_leaves_the_new_bot_unadoptable(db_path):
    """A different bot account under the same mind is a bot nobody has
    claimed. Adoption must not hand it the configured id, because the
    operator may have just pointed this mind at someone else's bot."""
    async def go():
        db = await _open(db_path)
        try:
            await surface_claims.record_bot(db, "m1", "telegram", "first_bot", now=1000.0)
            phrase = await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            await surface_claims.attempt_claim(
                db, "m1", "telegram", "111", phrase, now=1001.0
            )
            await surface_claims.record_bot(db, "m1", "telegram", "second_bot", now=1002.0)
            outcome = await surface_claims.adopt_claim(
                db, "m1", "telegram", "8776938611", now=1003.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1003.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is False
    assert state.claimed_ref == ""


def test_a_phrase_nobody_sent_stops_blocking_adoption_once_it_expires(db_path):
    """Nothing clears an expired phrase, so counting any phrase as a decision
    left a bot minted-and-forgotten permanently unadoptable — while the
    console read it as unclaimed with no phrase pending and offered minting
    another, which rebuilt the same row."""
    async def go():
        db = await _open(db_path)
        try:
            await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            expired = 1000.0 + surface_claims.PHRASE_TTL_SECONDS + 1.0
            outcome = await surface_claims.adopt_claim(
                db, "m1", "telegram", "8776938611", now=expired
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=expired)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is True
    assert state.claimed_ref == "8776938611"


def test_a_phrase_still_within_its_window_does_block_adoption(db_path):
    """The other side of the same line: a phrase somebody is about to send is
    a decision in progress, and a boot must not step on it."""
    async def go():
        db = await _open(db_path)
        try:
            await surface_claims.mint_phrase(db, "m1", "telegram", now=1000.0)
            outcome = await surface_claims.adopt_claim(
                db, "m1", "telegram", "8776938611", now=1001.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1001.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is False
    assert state.claimed_ref == ""


def test_an_unclaimed_row_written_before_the_marker_existed_is_treated_as_released(
    db_path,
):
    """On the old schema a release, a swapped bot account and a merely
    registered token all left `claimed_ref` NULL and nothing else. The two
    readings fail in opposite directions, and only one of them hands
    somebody else's bot over in silence."""
    async def go():
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute(
                "CREATE TABLE surface_claims ("
                "mind_id TEXT NOT NULL, surface TEXT NOT NULL, claimed_ref TEXT, "
                "claimed_at REAL, phrase_hash TEXT, phrase_issued_at REAL, "
                "phrase_expires_at REAL, PRIMARY KEY (mind_id, surface))"
            )
            await db.execute(
                "INSERT INTO surface_claims (mind_id, surface, claimed_ref) "
                "VALUES ('m1', 'telegram', NULL)"
            )
            await db.commit()
            await surface_claims.init(db)
            outcome = await surface_claims.adopt_claim(
                db, "m1", "telegram", "8776938611", now=1000.0
            )
            state = await surface_claims.claim_state(db, "m1", "telegram", now=1000.0)
            return outcome, state
        finally:
            await db.close()

    outcome, state = _run(go())
    assert outcome.claimed is False
    assert state.claimed_ref == ""


def test_the_gateway_s_own_startup_applies_the_claim_migrations(db_path):
    """The column this module grew has to exist on the database production
    actually opens. `CREATE TABLE IF NOT EXISTS` is a no-op against a table
    that is already there, so a schema change alone reaches nothing — and
    with the fixture calling `init` while the gateway called `executescript`,
    the whole suite stayed green over a column no live row had."""
    async def go():
        from comms import broker

        db = await aiosqlite.connect(db_path)
        try:
            # A table as it stood before this column, with a row in it.
            await db.execute(
                "CREATE TABLE surface_claims ("
                "mind_id TEXT NOT NULL, surface TEXT NOT NULL, claimed_ref TEXT, "
                "claimed_at REAL, phrase_hash TEXT, phrase_issued_at REAL, "
                "phrase_expires_at REAL, PRIMARY KEY (mind_id, surface))"
            )
            await db.execute(
                "INSERT INTO surface_claims (mind_id, surface, claimed_ref) "
                "VALUES ('m1', 'telegram', '111')"
            )
            await db.commit()
        finally:
            await db.close()

        live = await broker.init_db(db_path)
        try:
            cur = await live.execute("PRAGMA table_info(surface_claims)")
            columns = {row[1] for row in await cur.fetchall()}
            # And the route the console presses still works against it.
            state = await surface_claims.clear_claim(live, "m1", "telegram")
            return columns, state
        finally:
            await live.close()

    columns, state = _run(go())
    assert "released_at" in columns
    assert "phrase_attempts" in columns
    assert state.claimed_ref == ""
