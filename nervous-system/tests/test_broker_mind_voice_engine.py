"""The broker row caches which engine speaks a mind.

The mind's own `runtime.yaml` is the truth; this row is what every speaking
surface reads, because a surface cannot mount a file on another machine. A
registration that omits the engine leaves the stored one alone: a mind
re-registering from an older build must not blank a working voice.
"""

from __future__ import annotations

import pytest

from comms import broker
from comms_broker_pg import broker_store, scratch_dsn  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio


async def _registered(**overrides):
    fields = {
        "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f",
        "name": "skippy",
        "gateway_url": "http://192.168.4.64:8421",
        "model": "claude-opus-5",
        "harness": "claude_cli",
        "owner": "daniel",
    }
    fields.update(overrides)
    await broker.register_mind(**fields)
    return await broker.get_mind(fields["name"])


async def test_registration_stores_the_engine_and_the_voice(broker_store) -> None:
    row = await _registered(
        voice="_dramitac_mono_voice_ref.wav", voice_engine="chatterbox"
    )
    assert row["voice_engine"] == "chatterbox"
    assert row["voice"] == "_dramitac_mono_voice_ref.wav"


async def test_a_registration_omitting_the_engine_leaves_the_stored_one(
    broker_store,
) -> None:
    """A mind running an older build must not blank what the console set."""
    await _registered(voice_engine="kokoro", voice="af_heart")
    row = await _registered()
    assert row["voice_engine"] == "kokoro"
    assert row["voice"] == "af_heart"


async def test_a_mind_that_has_named_no_engine_stores_none(broker_store) -> None:
    """Absent rather than empty, so the reader falls through to its default."""
    row = await _registered()
    assert row["voice_engine"] is None


async def test_a_re_registration_that_changes_the_engine_updates_the_row(
    broker_store,
) -> None:
    """The primary path: a mind re-registers from its file on every boot.

    Every other test here lands on the INSERT, so the UPDATE branch was only
    ever exercised with the engine absent — and deleting it entirely left all
    five green while an engine edit in `runtime.yaml` never reached the row.
    """
    await _registered(voice_engine="chatterbox", voice="voice_ref.wav")
    row = await _registered(voice_engine="kokoro", voice="af_heart")
    assert row["voice_engine"] == "kokoro"
    assert row["voice"] == "af_heart"


async def test_the_engine_can_be_updated_without_restating_the_rest(
    broker_store,
) -> None:
    await _registered(voice_engine="chatterbox")
    updated = await broker.update_mind("skippy", voice_engine="kokoro")
    assert updated["voice_engine"] == "kokoro"
    assert updated["model"] == "claude-opus-5"
