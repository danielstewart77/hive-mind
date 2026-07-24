"""Session lineage — which session replaced which.

A surface that reattaches across a rotation has to find the session that
REPLACED the one it lost. Without a recorded link the only available
heuristic is "some other live session on the same mind", and with two
browser terminals open on one mind that heuristic picks the neighbour:
the reconnecting tile adopts a conversation it never owned, drags its own
name and colour onto it, and leaves the neighbour showing nothing.

``rotated_from`` makes the relationship a fact instead of a guess.
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


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(_registry())
    await mgr.start()
    return mgr


def _registry() -> ModelRegistry:
    return ModelRegistry({"anthropic": Provider(name="anthropic")}, {"opus": "anthropic"})


async def _seed(mgr: SessionManager, sid: str, *, client_ref: str,
                mind_id: str = "ada", rotated_from: str | None = None) -> str:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at,
                                 last_active, status, mind_id, rotated_from)
           VALUES (?, 'telegram', ?, 'opus', ?, ?, 'running', ?, ?)""",
        (sid, client_ref, now, now, mind_id, rotated_from),
    )
    await mgr._db.execute(
        """INSERT OR REPLACE INTO active_sessions (client_type, client_ref, session_id)
           VALUES ('telegram', ?, ?)""",
        (client_ref, sid),
    )
    await mgr._db.commit()
    return sid


def test_session_dict_exposes_lineage() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-old", client_ref="123")
                await _seed(mgr, "sess-new", client_ref="456", rotated_from="sess-old")

                assert (await mgr.get_session("sess-old"))["rotated_from"] is None
                assert (await mgr.get_session("sess-new"))["rotated_from"] == "sess-old"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_finalize_stamps_the_replaced_session_id() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                old = await _seed(mgr, "sess-old", client_ref="123")
                await mgr.arm_rotation("telegram", "123")

                captured: dict = {}

                async def fake_create_session(*, owner_type, owner_ref, client_ref,
                                              mind_id, rotated_from=None, **kw):
                    captured["rotated_from"] = rotated_from
                    return {"id": "sess-new"}

                async def fake_kill(session_id):
                    return {"id": session_id}

                mgr.create_session = fake_create_session
                mgr.kill_session = fake_kill

                session = await mgr.get_session(old)
                new_id = await mgr._finalize_armed_rotation(session)

                assert new_id == "sess-new"
                assert captured["rotated_from"] == old
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_lineage_distinguishes_a_successor_from_a_sibling() -> None:
    """The exact query a reattaching terminal runs.

    Two live sessions on one mind: only the one carrying the dead session's
    id is a successor. The sibling must not match, however recently active.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-dead", client_ref="111")
                await _seed(mgr, "sess-sibling", client_ref="222")
                await _seed(mgr, "sess-heir", client_ref="333", rotated_from="sess-dead")

                rows = await mgr.list_sessions()
                heirs = [r["id"] for r in rows if r.get("rotated_from") == "sess-dead"]

                assert heirs == ["sess-heir"]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_kill_reaches_the_mind_for_a_session_the_cache_never_saw() -> None:
    """Ending a browser-born session must still end its process.

    A terminal-born session never went through ``_spawn``, so the in-memory
    process cache has no entry for it — and after a hive-comms restart the
    cache is empty for every live session. The mind's web terminal keeps its
    pty alive until told otherwise, so skipping the HTTP kill leaks a live
    `claude` process per ended session.
    """
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-terminal", client_ref="123", mind_id="mind-uuid")
                assert "sess-terminal" not in mgr._procs

                async def fake_mind_row(mind_id):
                    assert mind_id == "mind-uuid"
                    return {"gateway_url": "http://mind.test:8431"}

                mgr._get_mind_row = fake_mind_row
                resolved = await mgr._mind_url_for_session("sess-terminal")

                assert resolved == "http://mind.test:8431"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_mind_url_lookup_gives_up_quietly_on_an_unknown_mind() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed(mgr, "sess-orphan", client_ref="123", mind_id="gone")

                async def missing(mind_id):
                    raise ValueError("Mind 'gone' not found in broker.minds")

                mgr._get_mind_row = missing

                assert await mgr._mind_url_for_session("sess-orphan") is None
                assert await mgr._mind_url_for_session("no-such-session") is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_migration_adds_lineage_to_a_preexisting_database() -> None:
    """An existing sessions.db predates the column; start() must add it."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "sessions.db")
            os.environ["SESSIONS_DB_PATH"] = db_path

            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """CREATE TABLE sessions (
                        id TEXT PRIMARY KEY, claude_sid TEXT,
                        owner_type TEXT NOT NULL, owner_ref TEXT NOT NULL,
                        summary TEXT DEFAULT 'New session', model TEXT,
                        autopilot INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL, last_active REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'running')"""
                )
                await db.commit()

            mgr = SessionManager(_registry())
            await mgr.start()
            try:
                cur = await mgr._db.execute("PRAGMA table_info(sessions)")
                columns = {row["name"] for row in await cur.fetchall()}
                assert "rotated_from" in columns
            finally:
                await mgr.shutdown()

    _run(scenario())
