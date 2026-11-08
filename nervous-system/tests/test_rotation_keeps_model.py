"""A rotation replaces the conversation. It never replaces the model.

The mind's default model is the answer to "what does a *new* conversation
start on" — the console edits it, and `create_session` reads it from the
broker row. A rotation is not a new conversation in that sense: it is the
same one, continued with a fresh context window. Re-resolving the default
there would drag a live conversation onto whatever the default happens to be
now, silently undoing both a `/model` switch and the model the user has been
talking to for hours.
"""
# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch

from comms.sessions import SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    execute,
    fetchval,
    make_manager,
    register_mind,
    scratch_dsn,
    seed_session,
)


def _run(coro):
    return asyncio.run(coro)


async def _seed_session(*, model: str) -> str:
    """A running telegram session on `model`, bound as the surface's active."""
    return await seed_session(
        "sess-1", owner_type="telegram", owner_ref="123", model=model,
        claude_sid="conv-old", bind=("telegram", "123"),
    )


@contextlib.asynccontextmanager
async def _mind_defaulting_to(model: str, spawns: list):
    """A registered mind whose configured default is `model`.

    The spawn is captured rather than performed — these tests own no mind
    container, and what the spawn was told is the point.
    """
    await register_mind("ada", model=model)

    async def blocks(**_kw):
        return "<soul>seed</soul>"

    async def spawn(_self, session_id, spawn_model, **_kw):
        spawns.append((session_id, spawn_model))

    with patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_spawn", spawn):
        yield


async def _model_of(session_id: str) -> str:
    return await fetchval("SELECT model FROM core.sessions WHERE id = $1", session_id) or ""


def test_rotation_carries_the_conversations_model_forward(comms_db) -> None:
    """The mind's default moved to sonnet mid-conversation; opus survives,
    on the same row, because an armed turn names the conversation's own model
    rather than re-resolving the default."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            spawns: list = []
            sid = await _seed_session(model="opus")
            await execute(
                "UPDATE core.sessions SET rotation_armed = 1 WHERE id = $1", sid
            )

            async def nothing(*_a, **_kw):
                return None

            async def handover(*_a, **_kw):
                return "before"

            async with _mind_defaulting_to("sonnet", spawns):
                mgr._kill_process = nothing  # type: ignore[assignment]
                mgr._render_handover_on_mind = handover  # type: ignore[assignment]
                mgr._clear_rotation_marker_on_mind = nothing  # type: ignore[assignment]
                session = await mgr._get_row(sid)
                same_id = await mgr._replace_conversation(
                    dict(session), model=session["model"],
                    effort=session.get("effort"), window=None, threshold=None,
                )

            assert same_id == sid
            assert await _model_of(sid) == "opus"
            assert spawns == [(sid, "opus")]
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_fresh_session_does_take_the_minds_current_default(comms_db) -> None:
    """Rotation is the exception — a new conversation still picks it up."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            spawns: list = []
            async with _mind_defaulting_to("sonnet", spawns):
                created = await mgr.create_session(
                    owner_type="telegram",
                    owner_ref="123",
                    client_ref="123",
                    mind_id="ada",
                )
            assert await _model_of(created["id"]) == "sonnet"
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_caller_specified_model_still_wins_for_a_new_session(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            spawns: list = []
            async with _mind_defaulting_to("sonnet", spawns):
                created = await mgr.create_session(
                    owner_type="telegram",
                    owner_ref="123",
                    client_ref="123",
                    model="opus",
                    mind_id="ada",
                )
            assert await _model_of(created["id"]) == "opus"
        finally:
            await close_manager(mgr)

    _run(scenario())
