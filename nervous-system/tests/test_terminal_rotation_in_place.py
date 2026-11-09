"""A terminal rotation waits for the user's next typed message.

Crossing the context threshold used to respawn the pane the instant the
summary finished composing. That put the swap wherever the user happened to
be — often mid-read, often while a background agent was still running — and
the conversation that replaced it opened with a summary and no idea why it
existed.

Arming separates the two halves. ``arm_rotation`` records that the
conversation under the row is full and stops there; the pane keeps running
what the user is looking at. ``fire_rotation`` is called by the pane's own
UserPromptSubmit hook on the next typed turn, and replaces the conversation
through ``_replace_conversation`` — the one mechanism every surface uses —
handing the pane the handover and that message together as its opening user
turn, with the soul beside it as a system prompt.
"""
# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch

import pytest

from comms.sessions import DEFAULT_HARNESS_OVERHEAD_TOKENS as OVERHEAD
from comms.sessions import ROTATION_ARMED, MindCallFailed, SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    execute,
    fetchrow,
    make_manager,
    register_mind,
    scratch_dsn,
    seed_session,
)

SEED = "<soul>who you are</soul>\n<session-memory>what just happened</session-memory>"
#: What the mind renders of the conversation being replaced.
HANDOVER = "we were halfway through the boiler"


def _run(coro):
    return asyncio.run(coro)


async def _seed_terminal_session() -> str:
    session_id = "sess-term"
    await register_mind("ada", model="opus", gateway_url="http://mind")
    await seed_session(
        session_id, surface="web", surface_id="terminal", model="opus",
        conversation_id="conv-old", terminal_held=True, bind=("web", session_id),
    )
    return session_id


@contextlib.contextmanager
def _a_mind_with_a_live_pane(rotated_ok: bool = True, raises: bool = False):
    """Composition stubbed; the respawn call is captured, not made."""
    calls: list[dict] = []

    async def blocks(**_kw):
        return SEED

    async def rotated(_self, **kw):
        calls.append(kw)
        if raises:
            raise MindCallFailed("the mind did not answer")
        return rotated_ok

    async def handover(_self, *_a, **_kw):
        return HANDOVER

    async def nothing(*_a, **_kw):
        return None

    with patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_render_handover_on_mind", handover), \
            patch.object(SessionManager, "_clear_rotation_marker_on_mind", nothing), \
            patch.object(SessionManager, "_rotate_pty_on_mind", rotated):
        yield calls


async def _row(session_id: str) -> dict:
    return await fetchrow(
        "SELECT conversation_id AS conversation_id, carry_forward, carry_forward_sid, "
        "rotation_armed FROM core.sessions WHERE id = $1",
        session_id,
    )


def test_crossing_the_threshold_prepares_a_rotation_without_replacing_the_pane(comms_db) -> None:
    """The conversation the user is typing into is left exactly where it was.
    Replacing it on the assistant's turn is the failure arming exists to
    prevent: the pane goes blank under somebody reading it."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            with _a_mind_with_a_live_pane() as respawns:
                result = await mgr.arm_rotation("web", session_id)

            assert result["ok"] is True
            assert respawns == [], "the pane was respawned at arming time"
            row = await _row(session_id)
            # Still on the conversation the user can see.
            assert row["conversation_id"] == "conv-old"
            assert row["rotation_armed"] == ROTATION_ARMED
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_mind_that_refuses_the_respawn_leaves_the_conversation_and_the_arm(comms_db) -> None:
    """A refusal is about a pane that may still be running. Nothing is
    replaced, the arm stays, and the message is answered where it was typed —
    so the next turn tries again instead of losing the conversation."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            await mgr.arm_rotation("web", session_id)

            with _a_mind_with_a_live_pane(raises=True):
                result = await mgr.fire_rotation(
                    "web", session_id, conversation_id="conv-old", prompt="carry on",
                )

            assert result["ok"] is False
            row = await _row(session_id)
            assert row["conversation_id"] == "conv-old"
            assert row["rotation_armed"] == ROTATION_ARMED
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_the_pane_opens_on_the_handover_and_the_typed_message_as_one_turn(comms_db) -> None:
    """What reaches the new harness is a user turn carrying the handover and
    the message together — not a system prompt, which submits nothing and
    reaches no transcript, so what the user typed would be gone. The soul
    rides beside it where a system prompt belongs."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            await mgr.arm_rotation("web", session_id)

            with _a_mind_with_a_live_pane() as respawns:
                result = await mgr.fire_rotation(
                    "web", session_id,
                    conversation_id="conv-old", prompt="what were we doing?",
                )

            assert result["rotated"] is True
            assert len(respawns) == 1
            call = respawns[0]
            assert call["user_prompt"] == f"{HANDOVER}\n\nwhat were we doing?"
            assert call["system_prompt"] == SEED
            row = await _row(session_id)
            # The row is the same row, on the conversation that was
            # started, holding the opening turn until a turn completes.
            assert row["conversation_id"] == call["new_conversation_id"]
            assert row["conversation_id"] != "conv-old"
            assert row["carry_forward"] == call["user_prompt"]
            assert row["rotation_armed"] == 0
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_a_fired_rotation_leaves_the_conversation_with_an_estimate_of_its_opening(comms_db) -> None:
    """The pane's new conversation is sized from what it was handed — the
    soul, the handover and the typed message — before its first reply, or the
    feed draws a conversation that is somehow already at its threshold."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            await execute(
                "UPDATE core.sessions SET harness = 'dsh' WHERE id = $1", session_id)
            await mgr.report_context(session_id, tokens=150_000, kind="usage",
                                     threshold=160_000, window=200_000,
                                     conversation_id="conv-old")
            await mgr.arm_rotation("web", session_id)
            with _a_mind_with_a_live_pane() as respawns:
                await mgr.fire_rotation(
                    "web", session_id,
                    conversation_id="conv-old", prompt="what were we doing? " * 50,
                )
            row = await fetchrow(
                "SELECT context_tokens, context_kind, context_model, "
                "context_threshold, context_opening_surface FROM core.sessions "
                "WHERE id = $1", session_id,
            )
            assert row["context_tokens"] == (
                len(respawns[0]["system_prompt"])
                + len(respawns[0]["user_prompt"])
            ) // 4 + OVERHEAD["dsh"]
            assert row["context_kind"] == "estimated"
            assert row["context_model"] == "opus"
            # Opened in the pane, so only the pane's report can teach.
            assert row["context_opening_surface"] == "pane"
            # Still the same model, so still the point it rotates at.
            assert row["context_threshold"] == 160_000
        finally:
            await close_manager(mgr)

    _run(scenario())


@pytest.mark.parametrize("pane_held_by", ["terminal_held", "terminal:tab-7"])
def test_a_chat_turn_never_replaces_the_conversation_a_pane_holds(pane_held_by, comms_db) -> None:
    """A console message or a model change runs a chat process on a
    conversation a pane holds, and its hook arms that row. Acting on the arm
    from here would respawn the pane under somebody mid-keystroke and throw
    away the xterm buffer. The pane's own typed turn is the only thing that
    swaps it."""
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            session_id = await _seed_terminal_session()
            if pane_held_by == "terminal_held":
                await execute(
                    "UPDATE core.sessions SET terminal_held = true, rotation_armed = 1 "
                    "WHERE id = $1", session_id)
            else:
                await execute(
                    "UPDATE core.sessions SET terminal_held = false, surface = $1, "
                    "surface_id = 'tab-7', rotation_armed = 1 WHERE id = $2",
                    pane_held_by, session_id)

            async def stop_spawn(*a, **k):
                raise RuntimeError("delivered on the conversation itself")

            mgr._spawn = stop_spawn  # type: ignore[assignment]
            with _a_mind_with_a_live_pane() as respawns, \
                    pytest.raises(RuntimeError, match="conversation itself"):
                async for _ in mgr.send_message(session_id, "hello"):
                    pass

            assert respawns == [], "a chat turn respawned the pane"
            row = await _row(session_id)
            assert row["conversation_id"] == "conv-old"
            assert row["rotation_armed"] == ROTATION_ARMED
        finally:
            await close_manager(mgr)

    _run(scenario())
