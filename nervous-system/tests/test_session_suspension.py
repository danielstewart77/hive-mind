"""Resumable suspension for terminal sessions."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest
from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


async def _manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    if mgr._reaper_task:
        mgr._reaper_task.cancel()
        mgr._reaper_task = None
    return mgr


async def _seed(mgr: SessionManager, session_id: str, status: str) -> None:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions
           (id, owner_type, owner_ref, model, claude_sid, created_at,
            last_active, status, mind_id)
           VALUES
           (?, 'web', 'terminal', 'opus', 'conversation-1', ?, ?, ?, 'skippy')""",
        (session_id, now - 60, now, status),
    )
    await mgr._db.execute(
        """INSERT INTO active_sessions (client_type, client_ref, session_id)
           VALUES ('web', 'terminal-old', ?)""",
        (session_id,),
    )
    await mgr._db.commit()


def test_suspend_releases_process_and_unbinds_session():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
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
                bindings = await mgr._db.execute_fetchall(
                    "SELECT session_id FROM active_sessions WHERE session_id = 'sess-1'"
                )
                assert bindings == []
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_resume_suspended_session_creates_fresh_binding_without_spawning():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                await _seed(mgr, "sess-2", "suspended")

                result = await mgr.resume_session("sess-2", "web", "terminal-new")

                assert result["status"] == "idle"
                bindings = await mgr._db.execute_fetchall(
                    """SELECT client_type, client_ref FROM active_sessions
                       WHERE session_id = 'sess-2'"""
                )
                assert [tuple(row) for row in bindings] == [("web", "terminal-new")]
                assert "sess-2" not in mgr._procs
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_closed_history_cannot_be_suspended_or_resumed():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                await _seed(mgr, "sess-3", "closed")
                with pytest.raises(ValueError, match="closed"):
                    await mgr.suspend_session("sess-3")
                with pytest.raises(ValueError, match="closed"):
                    await mgr.resume_session("sess-3", "web", "terminal-new")
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_suspended_binding_is_not_an_active_session():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                await _seed(mgr, "sess-4", "suspended")
                assert await mgr.get_active_session("web", "terminal-old") is None
            finally:
                await mgr.shutdown()

    _run(scenario())
