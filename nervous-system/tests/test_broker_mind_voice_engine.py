"""The broker row caches which engine speaks a mind.

The mind's own `runtime.yaml` is the truth; this row is what every speaking
surface reads, because a surface cannot mount a file on another machine. A
registration that omits the engine leaves the stored one alone: a mind
re-registering from an older build must not blank a working voice.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from comms import broker


def _run(coro):
    return asyncio.run(coro)


async def _registered(db, **overrides):
    fields = {
        "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f",
        "name": "skippy",
        "gateway_url": "http://192.168.4.64:8421",
        "model": "claude-opus-5",
        "harness": "claude_cli",
    }
    fields.update(overrides)
    await broker.register_mind(db, **fields)
    return await broker.get_mind(db, fields["name"])


def test_registration_stores_the_engine_and_the_voice() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = await broker.init_db(os.path.join(tmp, "broker.db"))
            try:
                row = await _registered(
                    db, voice="_dramitac_mono_voice_ref.wav", voice_engine="chatterbox"
                )
                assert row["voice_engine"] == "chatterbox"
                assert row["voice"] == "_dramitac_mono_voice_ref.wav"
            finally:
                await db.close()

    _run(scenario())


def test_a_registration_omitting_the_engine_leaves_the_stored_one() -> None:
    """A mind running an older build must not blank what the console set."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = await broker.init_db(os.path.join(tmp, "broker.db"))
            try:
                await _registered(db, voice_engine="kokoro", voice="af_heart")
                row = await _registered(db)
                assert row["voice_engine"] == "kokoro"
                assert row["voice"] == "af_heart"
            finally:
                await db.close()

    _run(scenario())


def test_a_mind_that_has_named_no_engine_stores_none() -> None:
    """Absent rather than empty, so the reader falls through to its default."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = await broker.init_db(os.path.join(tmp, "broker.db"))
            try:
                row = await _registered(db)
                assert row["voice_engine"] is None
            finally:
                await db.close()

    _run(scenario())


def test_a_re_registration_that_changes_the_engine_updates_the_row() -> None:
    """The primary path: a mind re-registers from its file on every boot.

    Every other test here lands on the INSERT, so the UPDATE branch was only
    ever exercised with the engine absent — and deleting it entirely left all
    five green while an engine edit in `runtime.yaml` never reached the row.
    """

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = await broker.init_db(os.path.join(tmp, "broker.db"))
            try:
                await _registered(db, voice_engine="chatterbox", voice="voice_ref.wav")
                row = await _registered(db, voice_engine="kokoro", voice="af_heart")
                assert row["voice_engine"] == "kokoro"
                assert row["voice"] == "af_heart"
            finally:
                await db.close()

    _run(scenario())


def test_the_engine_can_be_updated_without_restating_the_rest() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = await broker.init_db(os.path.join(tmp, "broker.db"))
            try:
                await _registered(db, voice_engine="chatterbox")
                updated = await broker.update_mind(db, "skippy", voice_engine="kokoro")
                assert updated["voice_engine"] == "kokoro"
                assert updated["model"] == "claude-opus-5"
            finally:
                await db.close()

    _run(scenario())


def test_an_existing_database_gains_the_column() -> None:
    """Every deployed broker predates it; a migration that skipped would make
    every registration fail on an unknown column."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broker.db")
            db = await broker.init_db(path)
            await db.execute("ALTER TABLE minds DROP COLUMN voice_engine")
            await db.commit()
            await db.close()

            db = await broker.init_db(path)
            try:
                row = await _registered(db, voice_engine="kokoro")
                assert row["voice_engine"] == "kokoro"
            finally:
                await db.close()

    _run(scenario())
