"""A rotation replaces the conversation. It never replaces the model.

The mind's default model is the answer to "what does a *new* conversation
start on" — the console edits it, and `create_session` reads it from the
broker row. A rotation is not a new conversation in that sense: it is the
same one, continued with a fresh context window. Re-resolving the default
there would drag a live conversation onto whatever the default happens to be
now, silently undoing both a `/model` switch and the model the user has been
talking to for hours.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from unittest.mock import patch

from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)



async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    return mgr


async def _seed_session(mgr: SessionManager, *, model: str) -> str:
    """A running telegram session on `model`, bound as the surface's active."""
    session_id = "sess-1"
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 created_at, last_active, status, mind_id)
           VALUES (?, 'telegram', '123', ?, 'conv-old', ?, ?, 'running', 'ada')""",
        (session_id, model, now, now),
    )
    await mgr._db.execute(
        """INSERT INTO active_sessions (client_type, client_ref, session_id)
           VALUES ('telegram', '123', ?)""",
        (session_id,),
    )
    await mgr._db.commit()
    return session_id


@contextlib.contextmanager
def _mind_defaulting_to(model: str, spawns: list):
    """A registered mind whose configured default is `model`.

    The spawn is captured rather than performed — these tests own no mind
    container, and what the spawn was told is the point.
    """
    async def mind_row(_db, mind_id):
        return {"name": mind_id, "model": model}

    async def blocks(**_kw):
        return "<soul>seed</soul>"

    async def spawn(_self, session_id, spawn_model, **_kw):
        spawns.append((session_id, spawn_model))

    with patch("comms.broker.get_mind_by_id", mind_row), \
            patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_spawn", spawn):
        yield


async def _model_of(mgr: SessionManager, session_id: str) -> str:
    cur = await mgr._db.execute(
        "SELECT model FROM sessions WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    return row["model"] if row else ""


def test_rotation_carries_the_conversations_model_forward() -> None:
    """The mind's default moved to sonnet mid-conversation; opus survives."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                spawns: list = []
                old = await _seed_session(mgr, model="opus")
                with _mind_defaulting_to("sonnet", spawns):
                    async def _kill(session_id: str, **_kw) -> None:
                        await mgr._db.execute(
                            "UPDATE sessions SET status = 'closed' WHERE id = ?",
                            (session_id,),
                        )
                        await mgr._db.commit()

                    mgr.kill_session = _kill  # type: ignore[assignment]
                    session = await mgr._get_row(old)
                    new_id = await mgr._finalize_rotation(session)

                assert new_id is not None
                assert await _model_of(mgr, new_id) == "opus"
                assert spawns == [(new_id, "opus")]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_fresh_session_does_take_the_minds_current_default() -> None:
    """Rotation is the exception — a new conversation still picks it up."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                spawns: list = []
                with _mind_defaulting_to("sonnet", spawns):
                    created = await mgr.create_session(
                        owner_type="telegram",
                        owner_ref="123",
                        client_ref="123",
                        mind_id="ada",
                    )
                assert await _model_of(mgr, created["id"]) == "sonnet"
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_caller_specified_model_still_wins_for_a_new_session() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                spawns: list = []
                with _mind_defaulting_to("sonnet", spawns):
                    created = await mgr.create_session(
                        owner_type="telegram",
                        owner_ref="123",
                        client_ref="123",
                        model="opus",
                        mind_id="ada",
                    )
                assert await _model_of(mgr, created["id"]) == "opus"
            finally:
                await mgr.shutdown()

    _run(scenario())
