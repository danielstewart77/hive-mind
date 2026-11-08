"""Resumable suspension for terminal sessions."""

from __future__ import annotations

import asyncio
import time

import pytest

from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    fetch,
    make_manager,
    scratch_dsn,
    seed_session,
)


def _run(coro):
    return asyncio.run(coro)


async def _manager():
    mgr = await make_manager()
    if mgr._dashboard_sweep_task:
        mgr._dashboard_sweep_task.cancel()
        mgr._dashboard_sweep_task = None
    return mgr


async def _seed(mgr, session_id: str, status: str) -> None:
    now = time.time()
    await seed_session(
        session_id, owner_type="web", owner_ref="terminal", model="opus",
        claude_sid="conversation-1", created_at=now - 60, last_active=now,
        status=status, mind_id="skippy", bind=("web", "terminal-old"),
    )


def test_suspend_releases_process_and_unbinds_session(comms_db):
    async def scenario():
        mgr = await _manager()
        released = []

        async def release(session_id, surface):
            released.append((session_id, surface))
            return True

        mgr.release_on_mind = release
        try:
            await _seed(mgr, "sess-1", "running")

            result = await mgr.suspend_session("sess-1")

            assert result["status"] == "suspended"
            assert released == [
                ("sess-1", "terminal"),
                ("sess-1", "stream"),
            ]
            row = await mgr.get_session("sess-1")
            assert row["status"] == "suspended"
            bindings = await fetch(
                "SELECT session_id FROM core.active_sessions WHERE session_id = 'sess-1'"
            )
            assert bindings == []
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_resume_suspended_session_creates_fresh_binding_without_spawning(comms_db):
    async def scenario():
        mgr = await _manager()
        try:
            await _seed(mgr, "sess-2", "suspended")

            result = await mgr.resume_session("sess-2", "web", "terminal-new")

            assert result["status"] == "idle"
            bindings = await fetch(
                """SELECT client_type, client_ref FROM core.active_sessions
                   WHERE session_id = 'sess-2'"""
            )
            assert [tuple(row.values()) for row in bindings] == [("web", "terminal-new")]
            assert "sess-2" not in mgr._procs
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_closed_history_cannot_be_suspended_or_resumed(comms_db):
    async def scenario():
        mgr = await _manager()
        try:
            await _seed(mgr, "sess-3", "closed")
            with pytest.raises(ValueError, match="closed"):
                await mgr.suspend_session("sess-3")
            with pytest.raises(ValueError, match="closed"):
                await mgr.resume_session("sess-3", "web", "terminal-new")
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_suspended_binding_is_not_an_active_session(comms_db):
    async def scenario():
        mgr = await _manager()
        try:
            await _seed(mgr, "sess-4", "suspended")
            assert await mgr.get_active_session("web", "terminal-old") is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_two_simultaneous_activations_spawn_one_harness(comms_db):
    """Double-tapping a suspended conversation must not start two processes.

    Both callers pass the ``status == 'idle' and id not in _procs`` guard
    while the first spawn is still awaiting the mind, so without a lock the
    conversation ends up with two harness processes appending to one
    transcript and the first orphaned where nothing can kill it.
    """

    async def scenario():
        mgr = await _manager()
        spawned: list[str] = []

        async def fake_spawn(session_id, model, **kwargs):
            await asyncio.sleep(0.05)  # the real spawn is an HTTP call
            spawned.append(session_id)
            mgr._procs[session_id] = object()

        mgr._spawn = fake_spawn
        try:
            await _seed(mgr, "sess-double", "suspended")

            await asyncio.gather(
                mgr.activate_session("sess-double", "telegram", "chat-1"),
                mgr.activate_session("sess-double", "telegram", "chat-1"),
            )

            assert spawned == ["sess-double"]
        finally:
            mgr._procs.clear()
            await close_manager(mgr)

    _run(scenario())
