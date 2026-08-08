"""A terminal rotation waits for the user's next typed message.

Crossing the context threshold used to respawn the pane the instant the
summary finished composing. That put the swap wherever the user happened to
be — often mid-read, often while a background agent was still running — and
the successor opened with a summary and no idea why it existed.

Staging separates the two halves. ``arm_rotation`` composes the carry-forward
and parks it on the row against a conversation id that has been minted but
never started; the pane keeps running what the user is looking at.
``fire_rotation`` is called by the pane's own UserPromptSubmit hook on the
next typed turn, and hands the successor the summary and that message
together as one opening user turn.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from unittest.mock import patch

from comms.sessions import ROTATION_ARMED_TERMINAL, SessionManager

SEED = "<soul>who you are</soul>\n<session-memory>what just happened</session-memory>"


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
                                 created_at, last_active, status, mind_id)
           VALUES (?, 'web', 'terminal', 'opus', 'conv-old', ?, ?, 'running', 'ada')""",
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
def _a_mind_with_a_live_pane(rotated_ok: bool = True):
    """Composition stubbed; the respawn call is captured, not made."""
    calls: list[dict] = []

    async def mind_row(_db, mind_id):
        return {"name": mind_id, "model": "opus"}

    async def blocks(**_kw):
        return SEED

    async def rotated(_self, **kw):
        calls.append(kw)
        return rotated_ok

    with patch("comms.broker.get_mind_by_id", mind_row), \
            patch("comms.bootstrap_loader.compose_prompt_blocks", blocks), \
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
    """Requirement 1: the summary is composed and stored, and the conversation
    the user is typing into is left exactly where it was."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane() as respawns:
                    result = await mgr.arm_rotation("web", session_id, surface="terminal")

                assert result["staged"] is True
                assert respawns == [], "the pane was respawned at staging time"
                row = await _row(mgr, session_id)
                # Still on the conversation the user can see.
                assert row["claude_sid"] == "conv-old"
                # The seed is on the row, keyed to the successor that has not
                # started yet — that is what survives the wait.
                assert row["carry_forward"] == SEED
                assert row["carry_forward_sid"] not in ("", None, "conv-old")
                assert row["rotation_armed"] == ROTATION_ARMED_TERMINAL
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_failed_respawn_leaves_the_conversation_the_message_was_typed_into() -> None:
    """Requirement 7: when the mind reports no live terminal, nothing is
    swapped and the session stays exactly as the user left it."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane():
                    await mgr.arm_rotation("web", session_id, surface="terminal")

                with _a_mind_with_a_live_pane(rotated_ok=False):
                    result = await mgr.fire_rotation(
                        "web", session_id, claude_sid="conv-old", prompt="carry on",
                    )

                assert result["ok"] is False
                row = await _row(mgr, session_id)
                # The pane is still running this conversation, so it is still
                # the one the message is answered in.
                assert row["claude_sid"] == "conv-old"
                # And still staged, so the next turn tries again rather than
                # needing another six minutes of composition.
                assert row["rotation_armed"] == ROTATION_ARMED_TERMINAL
                assert row["carry_forward"] == SEED
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_the_successor_opens_on_the_summary_and_the_typed_message_as_one_turn() -> None:
    """Requirement 4: what reaches the new harness is a user turn carrying the
    summary and the message together — not a system prompt, which submits
    nothing and reaches no transcript."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane():
                    await mgr.arm_rotation("web", session_id, surface="terminal")
                staged_sid = (await _row(mgr, session_id))["carry_forward_sid"]

                with _a_mind_with_a_live_pane() as respawns:
                    result = await mgr.fire_rotation(
                        "web", session_id,
                        claude_sid="conv-old", prompt="what were we doing?",
                    )

                assert result["rotated"] is True
                assert len(respawns) == 1
                call = respawns[0]
                assert call["user_prompt"] == f"{SEED}\n\nwhat were we doing?"
                assert not call.get("system_prompt")
                # The conversation started is the one the seed was composed
                # for, so the seed stored at staging time is still its seed.
                assert call["new_claude_sid"] == staged_sid
                row = await _row(mgr, session_id)
                assert row["claude_sid"] == staged_sid
                assert row["rotation_armed"] == 0
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_a_chat_surface_adopting_a_staged_terminal_does_not_retire_its_session() -> None:
    """Requirement 1, the other half: staging marks the row, and that mark must
    not be mistaken for a chat surface's pending rotation. Finalizing one
    creates a successor row and kills this one — retiring the permanent
    terminal session that in-place rotation exists to preserve."""
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                session_id = await _seed_terminal_session(mgr)
                with _a_mind_with_a_live_pane():
                    await mgr.arm_rotation("web", session_id, surface="terminal")

                finalized: list[str] = []

                async def _finalize(_self, session):
                    finalized.append(session["id"])
                    return None

                with patch.object(SessionManager, "_finalize_rotation", _finalize):
                    # The rotation branch is the third thing send_message does;
                    # everything after it wants a live mind to dispatch to, and
                    # that is not what is under test here.
                    with contextlib.suppress(Exception):
                        async for _ in mgr.send_message(session_id, "hello"):
                            break

                assert finalized == [], "a staged terminal was finalized as a chat rotation"
            finally:
                await mgr.shutdown()

    _run(scenario())
