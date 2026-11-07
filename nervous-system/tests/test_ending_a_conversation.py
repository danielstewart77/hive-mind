"""Ending a conversation removes it, entirely or not at all.

`kill_session` used to mark the row `closed` and keep it. 2,866 of the live
store's 3,046 rows were those tombstones, read by nothing: a rotation no
longer leaves a predecessor behind, every listing filters them out, and what
was durable went to the hive's memory through the Stop hook while the
harness's own transcript stayed on the mind's disk.

Deleting instead has two edges worth guarding. The three deletes have to be
one transaction, because `session_turns` and `active_sessions` reference
`sessions(id)` with foreign keys enforced and no cascade — so a turn arriving
from a mind's Stop hook between the first delete and the last fails the row
delete, and committing the first two anyway leaves a conversation with its
turns and its binding gone and its row still marked running. And a kill takes
no turn lock, so it lands mid-turn: the path that respawns a harness the mind
has forgotten reads a missing row as final, exactly as it read a closed one,
or it starts a fresh process on the transcript of the conversation just
ended. That branch is reached only by a kill landing inside a live turn's
HTTP call to its mind, which needs a concurrency harness this suite does not
have — it is guarded by the `if not fresh or ... == "closed"` read in
`_send_locked` and by nothing here.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()

    async def no_process(session_id: str) -> None:
        mgr._procs.pop(session_id, None)
        mgr._mind_ids.pop(session_id, None)

    mgr._kill_process = no_process  # type: ignore[assignment]
    return mgr


async def _seed(mgr: SessionManager, session_id: str = "sess-1") -> str:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 created_at, last_active, status, mind_id,
                                 name, color, summary)
           VALUES (?, 'telegram', '123', 'opus', 'conv-1', ?, ?, 'running',
                   'ada', 'Fittimus Maximus', '#3481cc', 'talking')""",
        (session_id, now, now),
    )
    await mgr._db.execute(
        """INSERT INTO active_sessions (client_type, client_ref, session_id)
           VALUES ('telegram', '123', ?)""",
        (session_id,),
    )
    await mgr._db.execute(
        "INSERT INTO session_turns (session_id, role, content, created_at) "
        "VALUES (?, 'user', 'something said', ?)",
        (session_id, now),
    )
    await mgr._db.commit()
    return session_id


async def _counts(mgr: SessionManager, session_id: str) -> dict:
    out = {}
    for table, column in (
        ("sessions", "id"), ("session_turns", "session_id"),
        ("active_sessions", "session_id"),
    ):
        cur = await mgr._db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (session_id,)
        )
        out[table] = (await cur.fetchone())["n"]
    return out


def test_the_kill_still_reports_the_conversations_name_and_colour() -> None:
    """The surface reporting a kill calls the conversation what the button
    that killed it called it. The row is read before the deletes for exactly
    that reason — read after, every caller falls back to the summary, which
    for a browser-terminal conversation is the literal "New session" forever.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr)
                killed = await mgr.kill_session("sess-1")
                assert killed["name"] == "Fittimus Maximus"
                assert killed["color"] == "#3481cc"
                assert killed["status"] == "closed"
                assert await mgr._get_row("sess-1") is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_turn_landing_between_the_deletes_leaves_the_conversation_whole() -> None:
    """Every mind's Stop hook writes to `session_turns` over HTTP, so a turn
    genuinely arrives mid-kill. The row delete then fails its foreign key,
    and all three deletes have to go back — a conversation with its turns and
    its binding gone but its row still running is listed as live and
    respawned by the next message."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr)
                real_execute = mgr._db.execute
                landed = {"done": False}

                async def execute_and_interleave(sql, *args, **kwargs):
                    result = await real_execute(sql, *args, **kwargs)
                    if "DELETE FROM active_sessions" in sql and not landed["done"]:
                        landed["done"] = True
                        await real_execute(
                            "INSERT INTO session_turns (session_id, role, content, "
                            "created_at) VALUES ('sess-1', 'assistant', 'mid-turn', ?)",
                            (time.time(),),
                        )
                    return result

                mgr._db.execute = execute_and_interleave  # type: ignore[assignment]
                with pytest.raises(Exception):
                    await mgr.kill_session("sess-1")
                mgr._db.execute = real_execute  # type: ignore[assignment]

                assert landed["done"], "the interleaved turn never ran"
                # Nothing partial: the row, its ledger and its binding are all
                # still there, so the conversation can be ended again.
                counts = await _counts(mgr, "sess-1")
                assert counts["sessions"] == 1
                assert counts["session_turns"] >= 1
                assert counts["active_sessions"] == 1
                row = await mgr._get_row("sess-1")
                assert row["status"] == "running"
            finally:
                await mgr.shutdown()

    _run(scenario())
