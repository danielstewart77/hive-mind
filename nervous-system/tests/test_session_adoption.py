"""Picking up a session on a different surface than you started it on.

A conversation started in the browser terminal was invisible from Telegram:
the surfaces already share the conversation id and the transcript, but the
session row is scoped by owner_ref, so a chat could only see what the chat
had started. You had to walk back to a browser to continue what you started
at the desk.

Adoption closes that. The picker lists what another surface is holding, and
switching to one hands it over: the outgoing process ends (two harness
processes on one conversation each hold it in memory, neither sees the
other's turns), the row is retargeted so replies follow, and the incoming
surface resumes the transcript with everything the other surface said.

Covers:
- terminal-born sessions appear in another surface's picker, flagged.
- machine-driven surfaces (broker, scheduler) are never offered.
- another mind's terminal sessions are not offered.
- closed sessions are not offered.
- adopting releases the terminal, retargets the row, and leaves one binding.
- a plain switch between your own sessions releases nothing.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

from comms.models import ModelRegistry, Provider
from comms.sessions import SessionManager

TELEGRAM = "telegram:skippy-uuid"
CHAT = "8776938611"


def _run(coro):
    return asyncio.run(coro)


def _registry() -> ModelRegistry:
    return ModelRegistry({"anthropic": Provider(name="anthropic")}, {"opus": "anthropic"})


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(_registry())
    await mgr.start()
    if mgr._reaper_task:
        mgr._reaper_task.cancel()
        mgr._reaper_task = None
    return mgr


async def _seed(
    mgr: SessionManager,
    session_id: str,
    *,
    owner_type: str = "web",
    owner_ref: str = "terminal",
    status: str = "running",
    mind_id: str = "skippy-uuid",
    claude_sid: str = "conv-1",
    summary: str = "New session",
) -> None:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions
           (id, owner_type, owner_ref, model, claude_sid, summary, created_at, last_active, status, mind_id)
           VALUES (?, ?, ?, 'opus', ?, ?, ?, ?, ?, ?)""",
        (session_id, owner_type, owner_ref, claude_sid, summary, now - 60, now, status, mind_id),
    )
    await mgr._db.commit()


async def _row(mgr: SessionManager, session_id: str) -> dict:
    rows = await mgr._db.execute_fetchall("SELECT * FROM sessions WHERE id = ?", (session_id,))
    return dict(rows[0])


async def _bindings(mgr: SessionManager, session_id: str) -> list[tuple[str, str]]:
    rows = await mgr._db.execute_fetchall(
        "SELECT client_type, client_ref FROM active_sessions WHERE session_id = ?", (session_id,)
    )
    return [(r["client_type"], r["client_ref"]) for r in rows]


def _selectable_ids(sessions: list[dict]) -> set[str]:
    return {s["id"] for s in sessions}


def test_terminal_sessions_are_offered_to_another_surface():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "own-1", owner_type=TELEGRAM, owner_ref=CHAT)
                await _seed(mgr, "term-1", summary="the thing I started at the desk")

                sessions = await mgr.list_selectable_sessions(
                    owner_ref=CHAT, client_type=TELEGRAM, client_ref=CHAT, mind_id="skippy-uuid"
                )

                assert _selectable_ids(sessions) == {"own-1", "term-1"}
                by_id = {s["id"]: s for s in sessions}
                assert by_id["term-1"]["adoptable"] is True
                assert by_id["term-1"]["surface"] == "terminal"
                assert by_id["own-1"]["adoptable"] is False
                assert by_id["own-1"]["surface"] == "telegram"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_machine_driven_sessions_are_never_offered():
    # A broker errand or a scheduled run is nobody's conversation to take.
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "broker-1", owner_type="broker", owner_ref="broker-abc")
                await _seed(mgr, "sched-1", owner_type="scheduler", owner_ref="scheduler")

                sessions = await mgr.list_selectable_sessions(
                    owner_ref=CHAT, client_type=TELEGRAM, client_ref=CHAT, mind_id="skippy-uuid"
                )

                assert sessions == []
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_another_minds_terminal_is_not_offered():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "mine", mind_id="skippy-uuid")
                await _seed(mgr, "bobs", mind_id="bob-uuid")

                sessions = await mgr.list_selectable_sessions(
                    owner_ref=CHAT, client_type=TELEGRAM, client_ref=CHAT, mind_id="skippy-uuid"
                )

                assert _selectable_ids(sessions) == {"mine"}
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_closed_terminal_sessions_are_not_offered():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "live", status="idle")
                await _seed(mgr, "done", status="closed")

                sessions = await mgr.list_selectable_sessions(
                    owner_ref=CHAT, client_type=TELEGRAM, client_ref=CHAT, mind_id="skippy-uuid"
                )

                assert _selectable_ids(sessions) == {"live"}
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_adopting_releases_the_terminal_and_retargets_the_session():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "term-1")
                await mgr._db.execute(
                    """INSERT INTO active_sessions (client_type, client_ref, session_id)
                       VALUES ('web', 'terminal-tile-1', 'term-1')"""
                )
                await mgr._db.commit()

                released: list[tuple[str, str]] = []

                async def _release(session_id, surface):
                    released.append((session_id, surface))
                    return True

                spawned: list[str] = []

                async def _spawn(session_id, *a, **kw):
                    spawned.append(session_id)
                    return {}

                mgr.release_on_mind = _release
                mgr._spawn = _spawn

                await mgr.activate_session(
                    "term-1", TELEGRAM, CHAT, owner_type=TELEGRAM, owner_ref=CHAT
                )

                # The outgoing process ends — one conversation, one writer.
                assert released == [("term-1", "terminal")]
                # ...and the incoming surface resumes the same conversation.
                assert spawned == ["term-1"]
                row = await _row(mgr, "term-1")
                assert row["owner_type"] == TELEGRAM
                assert row["owner_ref"] == CHAT
                assert row["claude_sid"] == "conv-1"  # the thread is untouched
                # Exactly one surface holds it now.
                assert await _bindings(mgr, "term-1") == [(TELEGRAM, CHAT)]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_switching_between_your_own_sessions_releases_nothing():
    # Ordinary /switch is not a handover and must not kill anything.
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "own-1", owner_type=TELEGRAM, owner_ref=CHAT)
                released: list = []

                async def _release(session_id, surface):
                    released.append((session_id, surface))
                    return True

                mgr.release_on_mind = _release

                await mgr.activate_session(
                    "own-1", TELEGRAM, CHAT, owner_type=TELEGRAM, owner_ref=CHAT
                )

                assert released == []
                assert await _bindings(mgr, "own-1") == [(TELEGRAM, CHAT)]
            finally:
                await mgr.shutdown()

    _run(scenario())
