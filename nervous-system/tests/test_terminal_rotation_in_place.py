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
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from comms.sessions import DEFAULT_HARNESS_OVERHEAD_TOKENS as OVERHEAD
from comms.sessions import ROTATION_ARMED, MindCallFailed, SessionManager

SEED = "<soul>who you are</soul>\n<session-memory>what just happened</session-memory>"
#: What the mind renders of the conversation being replaced.
HANDOVER = "we were halfway through the boiler"


def _run(coro):
    return asyncio.run(coro)


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    return mgr


async def _seed_terminal_session(mgr: SessionManager) -> str:
    session_id = "sess-term"
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 created_at, last_active, status, mind_id,
                                 terminal_held)
           VALUES (?, 'web', 'terminal', 'opus', 'conv-old', ?, ?, 'running', 'ada', 1)""",
        (session_id, now, now),
    )
    await mgr._db.execute(
        """INSERT INTO active_sessions (client_type, client_ref, session_id)
           VALUES ('web', ?, ?)""",
        (session_id, session_id),
    )
    await mgr._db.commit()
    return session_id


@contextlib.contextmanager
def _a_mind_with_a_live_pane(rotated_ok: bool = True, raises: bool = False):
    """Composition stubbed; the respawn call is captured, not made."""
    calls: list[dict] = []

    async def mind_row(_db, mind_id):
        return {"name": mind_id, "model": "opus", "gateway_url": "http://mind"}

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

    with patch("comms.broker.get_mind_by_id", mind_row), \
            patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
            patch.object(SessionManager, "_render_handover_on_mind", handover), \
            patch.object(SessionManager, "_clear_rotation_marker_on_mind", nothing), \
            patch.object(SessionManager, "_rotate_pty_on_mind", rotated):
        yield calls


async def _row(mgr: SessionManager, session_id: str) -> dict:
    cur = await mgr._db.execute(
        "SELECT claude_sid, carry_forward, carry_forward_sid, rotation_armed "
        "FROM sessions WHERE id = ?",
        (session_id,),
    )
    return dict(await cur.fetchone())


def test_crossing_the_threshold_prepares_a_rotation_without_replacing_the_pane() -> None:
    """The conversation the user is typing into is left exactly where it was.
    Replacing it on the assistant's turn is the failure arming exists to
    prevent: the pane goes blank under somebody reading it."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane() as respawns:
                    result = await mgr.arm_rotation("web", session_id)

                assert result["ok"] is True
                assert respawns == [], "the pane was respawned at arming time"
                row = await _row(mgr, session_id)
                # Still on the conversation the user can see.
                assert row["claude_sid"] == "conv-old"
                assert row["rotation_armed"] == ROTATION_ARMED
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_mind_that_refuses_the_respawn_leaves_the_conversation_and_the_arm() -> None:
    """A refusal is about a pane that may still be running. Nothing is
    replaced, the arm stays, and the message is answered where it was typed —
    so the next turn tries again instead of losing the conversation."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                await mgr.arm_rotation("web", session_id)

                with _a_mind_with_a_live_pane(raises=True):
                    result = await mgr.fire_rotation(
                        "web", session_id, claude_sid="conv-old", prompt="carry on",
                    )

                assert result["ok"] is False
                row = await _row(mgr, session_id)
                assert row["claude_sid"] == "conv-old"
                assert row["rotation_armed"] == ROTATION_ARMED
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_the_pane_opens_on_the_handover_and_the_typed_message_as_one_turn() -> None:
    """What reaches the new harness is a user turn carrying the handover and
    the message together — not a system prompt, which submits nothing and
    reaches no transcript, so what the user typed would be gone. The soul
    rides beside it where a system prompt belongs."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                await mgr.arm_rotation("web", session_id)

                with _a_mind_with_a_live_pane() as respawns:
                    result = await mgr.fire_rotation(
                        "web", session_id,
                        claude_sid="conv-old", prompt="what were we doing?",
                    )

                assert result["rotated"] is True
                assert len(respawns) == 1
                call = respawns[0]
                assert call["user_prompt"] == f"{HANDOVER}\n\nwhat were we doing?"
                assert call["system_prompt"] == SEED
                row = await _row(mgr, session_id)
                # The row is the same row, on the conversation that was
                # started, holding the opening turn until a turn completes.
                assert row["claude_sid"] == call["new_claude_sid"]
                assert row["claude_sid"] != "conv-old"
                assert row["carry_forward"] == call["user_prompt"]
                assert row["rotation_armed"] == 0
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_fired_rotation_leaves_the_conversation_with_an_estimate_of_its_opening() -> None:
    """The pane's new conversation is sized from what it was handed — the
    soul, the handover and the typed message — before its first reply, or the
    feed draws a conversation that is somehow already at its threshold."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                await mgr._db.execute(
                    "UPDATE sessions SET harness = 'dsh' WHERE id = ?", (session_id,))
                await mgr._db.commit()
                await mgr.report_context(session_id, tokens=150_000, kind="usage",
                                         threshold=160_000, window=200_000,
                                         conversation_id="conv-old")
                await mgr.arm_rotation("web", session_id)
                with _a_mind_with_a_live_pane() as respawns:
                    await mgr.fire_rotation(
                        "web", session_id,
                        claude_sid="conv-old", prompt="what were we doing? " * 50,
                    )
                cur = await mgr._db.execute(
                    "SELECT context_tokens, context_kind, context_model, "
                    "context_threshold, context_opening_surface FROM sessions "
                    "WHERE id = ?", (session_id,),
                )
                row = dict(await cur.fetchone())
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
                await mgr.shutdown()

    _run(scenario())


@pytest.mark.parametrize("pane_held_by", ["terminal_held", "terminal:tab-7"])
def test_a_chat_turn_never_replaces_the_conversation_a_pane_holds(pane_held_by) -> None:
    """A console message or a model change runs a chat process on a
    conversation a pane holds, and its hook arms that row. Acting on the arm
    from here would respawn the pane under somebody mid-keystroke and throw
    away the xterm buffer. The pane's own typed turn is the only thing that
    swaps it."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                if pane_held_by == "terminal_held":
                    await mgr._db.execute(
                        "UPDATE sessions SET terminal_held = 1, rotation_armed = 1 "
                        "WHERE id = ?", (session_id,))
                else:
                    await mgr._db.execute(
                        "UPDATE sessions SET terminal_held = 0, owner_type = ?, "
                        "owner_ref = 'tab-7', rotation_armed = 1 WHERE id = ?",
                        (pane_held_by, session_id))
                await mgr._db.commit()

                async def stop_spawn(*a, **k):
                    raise RuntimeError("delivered on the conversation itself")

                mgr._spawn = stop_spawn  # type: ignore[assignment]
                with _a_mind_with_a_live_pane() as respawns, \
                        pytest.raises(RuntimeError, match="conversation itself"):
                    async for _ in mgr.send_message(session_id, "hello"):
                        pass

                assert respawns == [], "a chat turn respawned the pane"
                row = await _row(mgr, session_id)
                assert row["claude_sid"] == "conv-old"
                assert row["rotation_armed"] == ROTATION_ARMED
            finally:
                await mgr.shutdown()

    _run(scenario())
