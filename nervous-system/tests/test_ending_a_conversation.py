"""Ending a conversation removes it, entirely or not at all.

`kill_session` used to mark the row `closed` and keep it. 2,866 of the live
store's 3,046 rows were those tombstones, read by nothing: a rotation no
longer leaves a predecessor behind, every listing filters them out, and what
was durable went to the hive's memory through the Stop hook while the
harness's own transcript stayed on the mind's disk.

Deleting instead is one statement: the turn ledger and the surface binding
reference `sessions(id)` ON DELETE CASCADE, so they go with the row in the
same instant. A kill takes no turn lock, so it lands mid-turn: the path that
respawns a harness the mind has forgotten reads a missing row as final,
exactly as it read a closed one, or it starts a fresh process on the
transcript of the conversation just ended. That branch is reached only by a
kill landing inside a live turn's HTTP call to its mind, which needs a
concurrency harness this suite does not have — it is guarded by the
`if not fresh or ... == "closed"` read in `_send_locked` and by nothing here.
"""
from __future__ import annotations

import asyncio

from comms_pg_fixture import (  # noqa: F401 - fixtures
    add_turn,
    close_manager,
    comms_db,
    fetchval,
    make_manager,
    scratch_dsn,
    seed_session,
)


def _run(coro):
    return asyncio.run(coro)


async def _make_manager():
    mgr = await make_manager()

    async def no_process(session_id: str) -> None:
        mgr._procs.pop(session_id, None)
        mgr._mind_ids.pop(session_id, None)

    mgr._kill_process = no_process  # type: ignore[assignment]
    return mgr


async def _seed(session_id: str = "sess-1") -> str:
    await seed_session(
        session_id, owner_type="telegram", owner_ref="123", claude_sid="conv-1",
        name="Fittimus Maximus", color="#3481cc", summary="talking",
        bind=("telegram", "123"),
    )
    await add_turn(session_id, "user", "something said")
    return session_id


async def _counts(session_id: str) -> dict:
    out = {}
    for table, column in (
        ("sessions", "id"), ("session_turns", "session_id"),
        ("active_sessions", "session_id"),
    ):
        out[table] = await fetchval(
            f"SELECT COUNT(*) FROM core.{table} WHERE {column} = $1", session_id
        )
    return out


def test_the_kill_still_reports_the_conversations_name_and_colour(comms_db) -> None:
    """The surface reporting a kill calls the conversation what the button
    that killed it called it. The row is read before the delete for exactly
    that reason — read after, every caller falls back to the summary, which
    for a browser-terminal conversation is the literal "New session" forever.
    And the row, its ledger and its binding are all gone afterwards.
    """
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            await _seed()
            killed = await mgr.kill_session("sess-1")
            assert killed["name"] == "Fittimus Maximus"
            assert killed["color"] == "#3481cc"
            assert killed["status"] == "closed"
            assert await _counts("sess-1") == {
                "sessions": 0, "session_turns": 0, "active_sessions": 0,
            }
        finally:
            await close_manager(mgr)

    _run(scenario())
