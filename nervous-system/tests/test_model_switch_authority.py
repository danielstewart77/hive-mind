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

            with patch.object(mgr, "mind_model_row", new=recording("offer", {"name": "claude-sonnet-5"})), \
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


# ---------------------------------------------------------------------------
# Effort
# ---------------------------------------------------------------------------

OFFERED = [
    {"name": "claude-opus-5", "effort_levels": ["low", "medium", "high", "max"]},
    {"name": "claude-sonnet-5", "effort_levels": ["low", "high"]},
    {"name": "qwen35-131k", "effort_levels": []},
]


def _offering(mgr):
    return patch.object(mgr, "mind_models", new=AsyncMock(return_value=OFFERED))


def _quiet(mgr):
    return (
        patch.object(mgr, "_kill_process", new=AsyncMock()),
        patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})),
    )


async def _mind(mgr):
    """A real mind row, so `_spawn` itself runs and the wire is observable."""
    mgr._get_mind_row = AsyncMock(return_value={
        "gateway_url": "http://mind:8420", "name": "ada",
    })
    mgr.mind_auth_headers = AsyncMock(return_value={})


def _wire():
    """The mind's spawn route, recording each body it is posted."""
    posted: list[dict] = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def text(self):
            return ""

    class _Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, json=None, **kwargs):
            posted.append(json)
            return _Resp()

    return posted, patch("aiohttp.ClientSession", lambda *a, **k: _Http())


def test_setting_an_offered_effort_records_it_and_respawns_with_it():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            await _mind(mgr)
            posted, wired = _wire()
            kill, routing = _quiet(mgr)
            with _offering(mgr), kill as killed, routing, wired:
                await mgr.set_effort("sess-1", "High")
            row = await mgr._get_row("sess-1")
            assert row["effort"] == "high"
            assert killed.await_count == 1
            assert [body["effort"] for body in posted] == ["high"]
            assert posted[0]["resume_sid"] == "conv-1"
            await mgr.shutdown()

    _run(scenario())


@pytest.mark.parametrize("model,level", [
    ("claude-opus-5", "xhigh"),     # a level this model does not list
    ("qwen35-131k", "high"),        # a model that takes no effort at all
])
def test_an_effort_the_model_does_not_offer_is_refused_and_nothing_moves(model, level):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, model)
            await mgr._db.execute("UPDATE sessions SET effort = 'low' WHERE id = 'sess-1'")
            await mgr._db.commit()
            kill, routing = _quiet(mgr)
            with _offering(mgr), kill as killed, routing, \
                    patch.object(mgr, "_spawn", new=AsyncMock()) as spawned:
                with pytest.raises(ValueError):
                    await mgr.set_effort("sess-1", level)
            assert killed.await_count == 0
            assert spawned.await_count == 0
            assert (await mgr._get_row("sess-1"))["effort"] == "low"
            await mgr.shutdown()

    _run(scenario())


def test_an_effort_change_mid_answer_is_refused():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            kill, routing = _quiet(mgr)
            lock = mgr._locks.setdefault("sess-1", asyncio.Lock())
            async with lock:
                with _offering(mgr), kill as killed, routing, \
                        patch.object(mgr, "_spawn", new=AsyncMock()) as spawned:
                    with pytest.raises(ValueError):
                        await asyncio.wait_for(mgr.set_effort("sess-1", "high"), 5)
                assert killed.await_count == 0
                assert spawned.await_count == 0
            assert (await mgr._get_row("sess-1"))["effort"] is None
            await mgr.shutdown()

    _run(scenario())


def test_an_effort_change_holds_the_lock_through_kill_and_respawn():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            held: dict[str, bool] = {}

            def recording(step):
                async def record(*args, **kwargs):
                    held[step] = mgr._locks["sess-1"].locked()
                    await asyncio.sleep(0)
                return record

            with _offering(mgr), \
                    patch.object(mgr, "_kill_process", new=recording("kill")), \
                    patch.object(mgr, "_spawn", new=recording("spawn")), \
                    patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
                await mgr.set_effort("sess-1", "max")

            assert held == {"kill": True, "spawn": True}
            assert mgr._locks["sess-1"].locked() is False
            await mgr.shutdown()

    _run(scenario())


@pytest.mark.parametrize("target,kept", [
    ("claude-sonnet-5", "low"),     # offers the current level: kept
    ("qwen35-131k", None),          # offers none: back to the model's default
])
def test_a_model_switch_keeps_the_effort_only_where_the_new_model_offers_it(target, kept):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            await mgr._db.execute("UPDATE sessions SET effort = 'low' WHERE id = 'sess-1'")
            await mgr._db.commit()
            await _mind(mgr)
            posted, wired = _wire()
            kill, routing = _quiet(mgr)
            with _offering(mgr), kill, routing, wired:
                await mgr.switch_model("sess-1", target)
            assert (await mgr._get_row("sess-1"))["effort"] == kept
            assert [body["effort"] for body in posted] == [kept]
            await mgr.shutdown()

    _run(scenario())


def test_a_respawn_from_nothing_carries_the_conversations_effort():
    """After a service restart nothing in memory remembers the level; the
    next spawn has to read it off the conversation."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            await mgr._db.execute("UPDATE sessions SET effort = 'max' WHERE id = 'sess-1'")
            await mgr._db.commit()
            await _mind(mgr)
            posted, wired = _wire()
            with wired:
                await mgr._spawn("sess-1", "claude-opus-5", resume_sid="conv-1", mind_id="ada")
            assert posted[0]["effort"] == "max"
            await mgr.shutdown()

    _run(scenario())


def test_a_rotation_successor_inherits_the_effort():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            await mgr._db.execute("UPDATE sessions SET effort = 'high' WHERE id = 'sess-1'")
            await mgr._db.commit()
            await _mind(mgr)
            posted, wired = _wire()
            with wired, \
                    patch("comms.broker.get_mind_by_id", new=AsyncMock(return_value={"name": "ada"})), \
                    patch("comms.bootstrap_loader.compose_prompt_blocks", new=AsyncMock(return_value="")):
                new = await mgr.create_session(
                    owner_type="telegram", owner_ref="123", client_ref="123",
                    model="claude-opus-5", mind_id="ada", rotated_from="sess-1",
                )
            assert (await mgr._get_row(new["id"]))["effort"] == "high"
            assert posted[-1]["effort"] == "high"
            await mgr.shutdown()

    _run(scenario())


def test_the_effort_options_are_the_models_levels_and_the_current_one():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            await mgr._db.execute("UPDATE sessions SET effort = 'medium' WHERE id = 'sess-1'")
            await mgr._db.commit()
            with _offering(mgr):
                options = await mgr.effort_options("sess-1")
            assert options["levels"] == ["low", "medium", "high", "max"]
            assert options["current"] == "medium"
            await mgr.shutdown()

    _run(scenario())


def test_an_unreadable_model_list_is_said_rather_than_read_as_no_effort():
    """An empty listing is a mind or proxy that could not be asked."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            with patch.object(mgr, "mind_models", new=AsyncMock(return_value=[])):
                with pytest.raises(ValueError, match="Couldn't read") as unreadable:
                    await mgr.effort_options("sess-1")
                assert "no longer offered" not in str(unreadable.value)
                with pytest.raises(ValueError, match="Couldn't read"):
                    await mgr.set_effort("sess-1", "high")
            await mgr.shutdown()

    _run(scenario())


def test_a_model_no_longer_offered_is_said_rather_than_read_as_unreadable():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-4-8")
            with _offering(mgr):
                with pytest.raises(ValueError, match="no longer offered") as withdrawn:
                    await mgr.effort_options("sess-1")
                assert "Couldn't read" not in str(withdrawn.value)
            await mgr.shutdown()

    _run(scenario())
