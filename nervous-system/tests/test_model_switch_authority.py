"""Which models exist is the proxy's business, asked through the mind.

The gateway used to hold a table mapping three short aliases to a provider,
with everything unrecognised falling through to Ollama — so a real deployment
name was classified as a local model, and a name nothing serves was accepted
outright. It now holds no such table: a mind reports what its own proxy key
can address, and a switch to anything else is refused rather than spawned.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest

from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


async def _manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    return mgr


async def _seed(mgr: SessionManager, model: str) -> str:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 created_at, last_active, status, mind_id)
           VALUES ('sess-1', 'telegram', '123', ?, 'conv-1', ?, ?, 'running', 'ada')""",
        (model, now, now),
    )
    await mgr._db.commit()
    return "sess-1"


def test_a_model_the_mind_does_not_offer_is_refused():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            with patch.object(
                mgr, "mind_models",
                new=AsyncMock(return_value=[{"name": "claude-opus-5"}]),
            ), patch.object(mgr, "_kill_process", new=AsyncMock()) as killed, \
                    patch.object(mgr, "_spawn", new=AsyncMock()) as spawned:
                with pytest.raises(ValueError):
                    await mgr.switch_model("sess-1", "gpt-5.4")
            # Refused before anything was torn down: the conversation the user
            # is in must survive a mistyped model name.
            assert killed.await_count == 0
            assert spawned.await_count == 0
            row = await mgr._get_row("sess-1")
            assert row["model"] == "claude-opus-5"
            await mgr.shutdown()

    _run(scenario())


def test_a_model_the_mind_offers_is_switched_to():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            with patch.object(
                mgr, "mind_models",
                new=AsyncMock(return_value=[
                    {"name": "claude-opus-5"}, {"name": "claude-sonnet-5"},
                ]),
            ), patch.object(mgr, "_kill_process", new=AsyncMock()) as killed, \
                    patch.object(mgr, "_spawn", new=AsyncMock()) as spawned, \
                    patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
                await mgr.switch_model("sess-1", "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            assert row["model"] == "claude-sonnet-5"
            # The switch is a teardown and a respawn, not a column write: a
            # row updated without the process being replaced is a conversation
            # reporting a model it is not running.
            assert killed.await_count == 1
            assert spawned.await_count == 1
            assert spawned.await_args.args[1] == "claude-sonnet-5"
            # Same conversation, new model: a respawn without the transcript
            # is a blank conversation reporting a successful switch.
            assert spawned.await_args.kwargs["resume_sid"] == "conv-1"
            await mgr.shutdown()

    _run(scenario())


def test_an_unreachable_mind_refuses_rather_than_approving_everything():
    """An empty listing must not become a blanket yes."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            with patch.object(mgr, "mind_models", new=AsyncMock(return_value=[])), \
                    patch.object(mgr, "_kill_process", new=AsyncMock()), \
                    patch.object(mgr, "_spawn", new=AsyncMock()):
                with pytest.raises(ValueError):
                    await mgr.switch_model("sess-1", "claude-sonnet-5")
            await mgr.shutdown()

    _run(scenario())


def test_a_switch_while_the_turn_is_still_streaming_is_refused():
    """The conversation keeps its model and its process until the turn ends.

    `send_message` holds the session's turn lock for the whole stream. A
    switch that ignored it killed the harness mid-answer: the reply being
    written was lost and the surface went on awaiting a stream that would
    never close, which reads as a mind that is still thinking.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            lock = mgr._locks.setdefault("sess-1", asyncio.Lock())
            async with lock:
                with patch.object(
                    mgr, "mind_models",
                    new=AsyncMock(return_value=[
                        {"name": "claude-opus-5"}, {"name": "claude-sonnet-5"},
                    ]),
                ), patch.object(mgr, "_kill_process", new=AsyncMock()) as killed, \
                        patch.object(mgr, "_spawn", new=AsyncMock()) as spawned, \
                        patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
                    with pytest.raises(ValueError):
                        # Bounded: a guard that queued behind the lock instead
                        # of refusing would otherwise hang the suite.
                        await asyncio.wait_for(
                            mgr.switch_model("sess-1", "claude-sonnet-5"), 5,
                        )
                assert killed.await_count == 0
                assert spawned.await_count == 0
            row = await mgr._get_row("sess-1")
            assert row["model"] == "claude-opus-5"
            await mgr.shutdown()

    _run(scenario())


def test_a_switch_once_the_turn_has_finished_goes_through():
    """The guard is the lock being held, not the lock existing.

    A session that has ever taken a turn keeps its lock object for the life of
    the process, so a guard that tested for presence rather than for being
    held would refuse every switch after the conversation's first turn.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            lock = mgr._locks.setdefault("sess-1", asyncio.Lock())
            async with lock:
                pass
            with patch.object(
                mgr, "mind_models",
                new=AsyncMock(return_value=[
                    {"name": "claude-opus-5"}, {"name": "claude-sonnet-5"},
                ]),
            ), patch.object(mgr, "_kill_process", new=AsyncMock()), \
                    patch.object(mgr, "_spawn", new=AsyncMock()), \
                    patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
                await mgr.switch_model("sess-1", "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            assert row["model"] == "claude-sonnet-5"
            await mgr.shutdown()

    _run(scenario())


@pytest.mark.parametrize("requested", [
    "claude-opus-5",        # a prefix of what is offered
    "claude-opus-5-5-x",    # what is offered is a prefix of it
    "Claude-Opus-5-5",      # the offered name in another case
])
def test_a_name_that_is_not_exactly_an_offered_model_is_refused(requested):
    """The check is an exact name, never a prefix.

    `mind_offers_model` compares the requested name against what the proxy
    said it serves. Loosened to a prefix match, "claude-opus-5" is approved
    against an offered "claude-opus-5-5" and then spawned verbatim — so the
    gate passes a name the proxy will reject, and the operator's switch
    reports success over a conversation that cannot answer. Every other test
    here asks for a name sharing no prefix with anything offered, which a
    prefix match satisfies too.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-sonnet-5-5")
            with patch.object(
                mgr, "mind_models",
                new=AsyncMock(return_value=[{"name": "claude-opus-5-5"}]),
            ), patch.object(mgr, "_kill_process", new=AsyncMock()) as killed, \
                    patch.object(mgr, "_spawn", new=AsyncMock()) as spawned:
                with pytest.raises(ValueError):
                    await mgr.switch_model("sess-1", requested)
            assert killed.await_count == 0
            assert spawned.await_count == 0
            row = await mgr._get_row("sess-1")
            assert row["model"] == "claude-sonnet-5-5"
            await mgr.shutdown()

    _run(scenario())


def test_the_lock_is_held_for_the_whole_switch_not_just_tested():
    """A turn arriving mid-switch must wait, not race the teardown.

    Testing the lock and proceeding is check-then-act across the `/models`
    round trip that follows, which runs to ten seconds. A message typed in
    that window passed `send_message`'s own idle check, took the lock and
    started streaming, and the switch killed it anyway — and because
    `send_message` decides whether to respawn on `session_id not in _procs`,
    which `_kill_process` had just emptied, that turn also spawned a second
    harness process on the same conversation.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            held: dict[str, bool] = {}

            def recording(step, result=None):
                async def record(*args, **kwargs):
                    # Each await is where a turn could slip in; the lock has
                    # to be ours at every one of them.
                    held[step] = mgr._locks["sess-1"].locked()
                    await asyncio.sleep(0)
                    return result
                return record

            with patch.object(mgr, "mind_offers_model", new=recording("offer", True)), \
                    patch.object(mgr, "_kill_process", new=recording("kill")), \
                    patch.object(mgr, "_spawn", new=recording("spawn")), \
                    patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
                await mgr.switch_model("sess-1", "claude-sonnet-5")

            assert held == {"offer": True, "kill": True, "spawn": True}
            # And released afterwards, or the conversation can never take
            # another turn.
            assert mgr._locks["sess-1"].locked() is False
            await mgr.shutdown()

    _run(scenario())


def test_an_autopilot_toggle_while_the_turn_is_streaming_is_refused():
    """The same teardown-and-respawn, and it had no guard at all.

    `/autopilot` typed during a streaming turn destroyed the answer with no
    race to lose, which is strictly easier to reach than the switch.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            lock = mgr._locks.setdefault("sess-1", asyncio.Lock())
            async with lock:
                with patch.object(mgr, "_kill_process", new=AsyncMock()) as killed, \
                        patch.object(mgr, "_spawn", new=AsyncMock()) as spawned, \
                        patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
                    with pytest.raises(ValueError):
                        await asyncio.wait_for(mgr.toggle_autopilot("sess-1"), 5)
                assert killed.await_count == 0
                assert spawned.await_count == 0
            row = await mgr._get_row("sess-1")
            assert row["autopilot"] == 0
            await mgr.shutdown()

    _run(scenario())


def test_an_autopilot_toggle_with_nothing_running_still_flips_and_respawns():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            with patch.object(mgr, "_kill_process", new=AsyncMock()) as killed, \
                    patch.object(mgr, "_spawn", new=AsyncMock()) as spawned, \
                    patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
                await mgr.toggle_autopilot("sess-1")
            row = await mgr._get_row("sess-1")
            assert row["autopilot"] == 1
            assert killed.await_count == 1
            assert spawned.await_count == 1
            await mgr.shutdown()

    _run(scenario())


def test_an_autopilot_toggle_holds_the_lock_through_kill_and_respawn():
    """Taken, not tested — the same race the switch closed."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            held: dict[str, bool] = {}
            spawned: dict = {}

            async def kill(*args, **kwargs):
                held["kill"] = mgr._locks["sess-1"].locked()
                await asyncio.sleep(0)

            async def spawn(*args, **kwargs):
                held["spawn"] = mgr._locks["sess-1"].locked()
                spawned.update(kwargs)
                await asyncio.sleep(0)

            with patch.object(mgr, "_kill_process", new=kill), \
                    patch.object(mgr, "_spawn", new=spawn), \
                    patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
                await mgr.toggle_autopilot("sess-1")

            assert held == {"kill": True, "spawn": True}
            assert mgr._locks["sess-1"].locked() is False
            # The process runs what the row says: a respawn on the old setting
            # leaves the row claiming autopilot over a process without it.
            assert spawned["autopilot"] is True
            assert spawned["resume_sid"] == "conv-1"
            await mgr.shutdown()

    _run(scenario())
