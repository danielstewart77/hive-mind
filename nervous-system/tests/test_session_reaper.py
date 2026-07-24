"""Stale-session reaper.

Nothing else ever closes an abandoned session: rotation and explicit kills
close their own, but a session whose surface walked away stays 'idle'
forever and reads as active everywhere downstream (the web terminal filed
56-day-old ghosts under Active). The reaper sweeps idle sessions past
REAP_IDLE_AFTER_SECONDS to 'closed' and drops their active_sessions
bindings.

Covers:
- stale idle sessions are closed and unbound; fresh idle ones untouched.
- a live tracked subprocess vetoes the timestamp, no matter how old.
- closed sessions are ignored (sweep is idempotent).
- start() launches the periodic reaper task; shutdown() cancels it.
- the loop's first sweep clears the backlog immediately, no interval wait.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

from comms.models import ModelRegistry, Provider
from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


def _registry() -> ModelRegistry:
    return ModelRegistry({"anthropic": Provider(name="anthropic")}, {"opus": "anthropic"})


async def _make_manager(tmp: str, *, stop_loop: bool = True) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(_registry())
    await mgr.start()
    if stop_loop:
        # Logic tests drive reap_stale_sessions by hand; the periodic
        # loop's immediate first sweep would race the seeds.
        mgr._reaper_task.cancel()
        mgr._reaper_task = None
    return mgr


async def _seed_session(
    mgr: SessionManager,
    session_id: str,
    *,
    status: str = "idle",
    idle_seconds: float = 0.0,
    bind_active: bool = False,
) -> None:
    now = time.time()
    last_active = now - idle_seconds
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
           VALUES (?, 'telegram:skippy', 'ref', 'opus', ?, ?, ?, 'skippy')""",
        (session_id, now - idle_seconds - 60, last_active, status),
    )
    if bind_active:
        await mgr._db.execute(
            """INSERT INTO active_sessions (client_type, client_ref, session_id)
               VALUES ('telegram:skippy', ?, ?)""",
            (session_id, session_id),
        )
    await mgr._db.commit()


async def _status(mgr: SessionManager, session_id: str) -> str:
    rows = await mgr._db.execute_fetchall(
        "SELECT status FROM sessions WHERE id = ?", (session_id,)
    )
    return rows[0]["status"]


async def _binding_count(mgr: SessionManager, session_id: str) -> int:
    rows = await mgr._db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM active_sessions WHERE session_id = ?", (session_id,)
    )
    return rows[0]["n"]


def test_reap_closes_stale_idle_and_drops_binding():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                stale_age = SessionManager.REAP_IDLE_AFTER_SECONDS + 3600
                await _seed_session(mgr, "sess-stale", idle_seconds=stale_age, bind_active=True)
                await _seed_session(mgr, "sess-fresh", idle_seconds=60)

                reaped = await mgr.reap_stale_sessions()

                assert reaped == ["sess-stale"]
                assert await _status(mgr, "sess-stale") == "closed"
                assert await _binding_count(mgr, "sess-stale") == 0
                assert await _status(mgr, "sess-fresh") == "idle"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_reap_skips_session_with_live_subprocess():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                stale_age = SessionManager.REAP_IDLE_AFTER_SECONDS + 3600
                await _seed_session(mgr, "sess-alive", idle_seconds=stale_age)
                mgr._procs["sess-alive"] = object()  # liveness beats the timestamp

                reaped = await mgr.reap_stale_sessions()

                assert reaped == []
                assert await _status(mgr, "sess-alive") == "idle"
            finally:
                mgr._procs.pop("sess-alive", None)
                await mgr.shutdown()

    _run(scenario())


def test_reap_ignores_closed_sessions():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                stale_age = SessionManager.REAP_IDLE_AFTER_SECONDS + 3600
                await _seed_session(mgr, "sess-closed", status="closed", idle_seconds=stale_age)

                first = await mgr.reap_stale_sessions()
                second = await mgr.reap_stale_sessions()

                assert first == []
                assert second == []
                assert await _status(mgr, "sess-closed") == "closed"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_start_launches_reaper_and_shutdown_cancels_it():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp, stop_loop=False)
            task = mgr._reaper_task
            try:
                assert task is not None
                assert not task.done()
            finally:
                await mgr.shutdown()
            assert mgr._reaper_task is None
            await asyncio.sleep(0)  # let cancellation propagate
            assert task.cancelled() or task.done()

    _run(scenario())


def test_reap_loop_first_sweep_runs_immediately():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                stale_age = SessionManager.REAP_IDLE_AFTER_SECONDS + 3600
                await _seed_session(mgr, "sess-backlog", idle_seconds=stale_age)

                # Restart the loop the way a service restart would: the first
                # sweep must clear the backlog without waiting an interval.
                mgr._reaper_task = asyncio.create_task(mgr._reap_loop())
                for _ in range(50):
                    if await _status(mgr, "sess-backlog") == "closed":
                        break
                    await asyncio.sleep(0.02)

                assert await _status(mgr, "sess-backlog") == "closed"
            finally:
                await mgr.shutdown()

    _run(scenario())
