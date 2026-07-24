"""Late-turn handoff — NS side.

Covers the durable session_turns watermark merge that makes session
rotation lossless:

- SessionManager.get_late_turns returns only the active session's turns
  committed after a watermark, from the session_turns ledger.
- bootstrap_loader surfaces the envelope's `continuation` list as a
  distinct <pending-continuation> block, single-source (never duplicated
  into <session-memory>), with no resurrection of answered turns.
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


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(_registry())
    await mgr.start()
    return mgr


async def _seed_session(mgr: SessionManager, *, owner_type: str, client_ref: str) -> str:
    """Insert a running session bound active to a surface. Returns session_id."""
    session_id = "sess-" + client_ref
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
           VALUES (?, ?, ?, 'opus', ?, ?, 'running', 'ada')""",
        (session_id, owner_type, client_ref, now, now),
    )
    await mgr._db.execute(
        """INSERT INTO active_sessions (client_type, client_ref, session_id)
           VALUES (?, ?, ?)""",
        (owner_type, client_ref, session_id),
    )
    await mgr._db.commit()
    return session_id


async def _add_turn(mgr: SessionManager, session_id: str, role: str, content: str, ts: float) -> None:
    await mgr._db.execute(
        "INSERT INTO session_turns (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_id, role, content, ts),
    )
    await mgr._db.commit()


# ---------------------------------------------------------------------------
# get_late_turns
# ---------------------------------------------------------------------------

def test_get_late_turns_returns_only_post_watermark() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_session(mgr, owner_type="telegram", client_ref="123")
                watermark = 1000.0
                # Before watermark — must be excluded.
                await _add_turn(mgr, sid, "user", "old question", watermark - 5)
                await _add_turn(mgr, sid, "assistant", "old answer", watermark - 4)
                # After watermark — must be included, oldest first.
                await _add_turn(mgr, sid, "user", "late question", watermark + 10)
                await _add_turn(mgr, sid, "assistant", "late answer", watermark + 11)

                result = await mgr.get_late_turns("telegram", "123", watermark)
                assert result["session_id"] == sid
                contents = [t["content"] for t in result["turns"]]
                assert contents == ["late question", "late answer"]
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_get_late_turns_no_active_session() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                result = await mgr.get_late_turns("telegram", "nobody", 0.0)
                assert result == {"session_id": None, "turns": []}
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_get_late_turns_ignores_closed_session() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_session(mgr, owner_type="telegram", client_ref="123")
                await _add_turn(mgr, sid, "user", "late", 1010.0)
                # Closed sessions are not active — get_active_session filters them,
                # so a post-clear query must not resurrect the dead session's turns.
                await mgr._db.execute(
                    "UPDATE sessions SET status = 'closed' WHERE id = ?", (sid,)
                )
                await mgr._db.commit()
                result = await mgr.get_late_turns("telegram", "123", 1000.0)
                assert result == {"session_id": None, "turns": []}
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_get_late_turns_scoped_to_one_session() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid_a = await _seed_session(mgr, owner_type="telegram", client_ref="aaa")
                sid_b = await _seed_session(mgr, owner_type="telegram", client_ref="bbb")
                await _add_turn(mgr, sid_a, "user", "for-a", 1010.0)
                await _add_turn(mgr, sid_b, "user", "for-b", 1010.0)
                result = await mgr.get_late_turns("telegram", "aaa", 1000.0)
                assert result["session_id"] == sid_a
                assert [t["content"] for t in result["turns"]] == ["for-a"]
            finally:
                await mgr.shutdown()

    _run(scenario())


# ---------------------------------------------------------------------------
# bootstrap_loader continuation rendering
# ---------------------------------------------------------------------------

def test_render_pending_continuation_user_turns() -> None:
    from comms import bootstrap_loader as bl

    block = bl._render_pending_continuation(
        [{"role": "user", "content": "deploy the thing"}]
    )
    assert "<pending-continuation>" in block
    assert "</pending-continuation>" in block
    assert "- deploy the thing" in block


def test_render_pending_continuation_empty() -> None:
    from comms import bootstrap_loader as bl

    assert bl._render_pending_continuation([]) == ""
    assert bl._render_pending_continuation(None) == ""
    # An assistant-only entry is not pending input.
    assert bl._render_pending_continuation([{"role": "assistant", "content": "x"}]) == ""


def test_fetch_session_memory_appends_continuation_no_duplication() -> None:
    """The continuation is single-source: rendered as <pending-continuation>,
    never duplicated into the <session-memory> body."""
    import json

    from comms import bootstrap_loader as bl

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                envelope = {
                    "carry_forward": "## Summary\nDid stuff.",
                    "continuation": [{"role": "user", "content": "pending ask"}],
                }
                await mgr._db.execute(
                    """INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at)
                       VALUES ('ada', 'Ada', 'cr', 'sid', ?, ?)""",
                    (json.dumps(envelope), time.time()),
                )
                await mgr._db.commit()

                out = await bl._fetch_session_memory(mgr._db, mind_id="ada", client_ref="cr")
                assert "<session-memory>" in out
                assert "Did stuff." in out
                assert "<pending-continuation>" in out
                assert "pending ask" in out
                # Single-source: "pending ask" appears exactly once.
                assert out.count("pending ask") == 1
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_fetch_session_memory_no_continuation_block_when_absent() -> None:
    import json

    from comms import bootstrap_loader as bl

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                envelope = {"carry_forward": "## Summary\nDid stuff."}
                await mgr._db.execute(
                    """INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at)
                       VALUES ('ada', 'Ada', 'cr', 'sid', ?, ?)""",
                    (json.dumps(envelope), time.time()),
                )
                await mgr._db.commit()
                out = await bl._fetch_session_memory(mgr._db, mind_id="ada", client_ref="cr")
                assert "<session-memory>" in out
                assert "<pending-continuation>" not in out
            finally:
                await mgr.shutdown()

    _run(scenario())
