"""A terminal's prose reaches the stream its tile's speaker listens to.

The speaker reads assistant events off ``/sessions/{id}/events``. Only
``send_message`` publishes those, and a pty-hosted conversation never calls
it, so every browser terminal was silent. The mind tails the harness's
transcript and posts each prose block here.

Requirement 5 — one turn is spoken once — lives on this side, because
ownership is what distinguishes "nothing else is publishing this" from
"``send_message`` already did".
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    return mgr


async def _session_row(mgr: SessionManager, session_id: str, owner_type: str) -> None:
    """A session row and nothing else.

    Written straight to the table rather than through ``create_session``,
    which spawns a harness against a mind that does not exist here. What is
    under test reads one column of it.
    """
    await mgr._db.execute(
        "INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, "
        "created_at, last_active, status, mind_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, "conv-1", owner_type, "ref-1", "opus",
         1_700_000_000.0, 1_700_000_000.0, "running", "mind-1"),
    )
    await mgr._db.commit()


def _capture(mgr: SessionManager) -> list[dict]:
    events: list[dict] = []

    async def _publish(_sid, event):
        events.append(event)

    mgr._publish_session_event = _publish
    return events


def test_a_terminal_pane_puts_its_prose_on_the_stream() -> None:
    """Requirement 1, gateway half: the block reaches the tile's stream.

    Marked complete, because these are whole blocks. A listener that waited
    out a quiet gap on them — which is exactly what the chat path needs —
    would put the speech a beat behind the writing, and being a beat behind
    is the behaviour this replaces.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _session_row(mgr, "sess-web", "web")
                events = _capture(mgr)
                result = await mgr.publish_pty_text("sess-web", "the build passed")
                return result, events
            finally:
                await mgr.shutdown()

    result, events = _run(scenario())
    assert result["ok"] is True
    assert len(events) == 1
    assert events[0]["type"] == "assistant"
    assert events[0]["content"] == "the build passed"
    assert events[0]["block_complete"] is True


def test_a_chat_owned_session_is_not_published_twice() -> None:
    """Requirement 5: one turn, spoken once.

    While Telegram drives, ``send_message`` publishes this same prose — the
    mind's tailer is reading the very transcript that process writes. Accept
    it here as well and a watching tile receives each sentence twice and
    says it twice in a row.
    """
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _session_row(mgr, "sess-tg", "telegram:mind-1")
                events = _capture(mgr)
                result = await mgr.publish_pty_text("sess-tg", "the build passed")
                return result, events
            finally:
                await mgr.shutdown()

    result, events = _run(scenario())
    assert result["ok"] is False
    assert events == [], "chat-owned prose was published a second time"


def test_whitespace_between_tool_calls_is_not_spoken() -> None:
    """A blank run between two tool calls is not a sentence, and a tile that
    receives it spends a voice-server round trip rendering silence."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _session_row(mgr, "sess-web", "web")
                events = _capture(mgr)
                result = await mgr.publish_pty_text("sess-web", "   \n  ")
                return result, events
            finally:
                await mgr.shutdown()

    result, events = _run(scenario())
    assert result["ok"] is False
    assert events == []
