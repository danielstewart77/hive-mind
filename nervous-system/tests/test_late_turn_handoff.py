"""Late-turn handoff — NS side.

Covers the durable session_turns watermark merge that makes session
rotation lossless:

- SessionManager.get_late_turns returns only the active session's turns
  committed after a watermark, from the session_turns ledger.
- bootstrap_loader surfaces the envelope's `continuation` list as a
  distinct <pending-continuation> block, single-source (never duplicated
  into <session-memory>), with no resurrection of answered turns.
"""
# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
import json

from comms_pg_fixture import (  # noqa: F401 - fixtures
    add_turn,
    close_manager,
    comms_db,
    execute,
    make_manager,
    register_mind,
    scratch_dsn,
    seed_session,
)


def _run(coro):
    return asyncio.run(coro)


async def _seed_session(*, surface: str, client_ref: str) -> str:
    """Insert a running session bound active to a surface. Returns session_id."""
    session_id = "sess-" + client_ref
    await seed_session(
        session_id, surface=surface, surface_id=client_ref,
        conversation_id="conv-" + client_ref, bind=(surface, client_ref),
    )
    return session_id


async def _seed_memory(body: str) -> None:
    await register_mind("ada", name="Ada")
    await execute(
        "INSERT INTO core.session_memory (mind_id, mind_name, client_ref, "
        "conversation_id, body, created_at) VALUES ('ada', 'Ada', 'cr', 'sid', $1, $2)",
        body, 1_800_000_000.0,
    )


# ---------------------------------------------------------------------------
# get_late_turns
# ---------------------------------------------------------------------------

def test_get_late_turns_returns_only_post_watermark(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            sid = await _seed_session(surface="telegram", client_ref="123")
            watermark = 1000.0
            # Before watermark — must be excluded.
            await add_turn(sid, "user", "old question", watermark - 5)
            await add_turn(sid, "assistant", "old answer", watermark - 4)
            # After watermark — must be included, oldest first.
            await add_turn(sid, "user", "late question", watermark + 10)
            await add_turn(sid, "assistant", "late answer", watermark + 11)

            result = await mgr.get_late_turns("telegram", "123", watermark)
            assert result["session_id"] == sid
            contents = [t["content"] for t in result["turns"]]
            assert contents == ["late question", "late answer"]
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_get_late_turns_no_active_session(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            result = await mgr.get_late_turns("telegram", "nobody", 0.0)
            assert result == {"session_id": None, "turns": []}
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_get_late_turns_ignores_closed_session(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            sid = await _seed_session(surface="telegram", client_ref="123")
            await add_turn(sid, "user", "late", 1010.0)
            # Closed sessions are not active — get_active_session filters them,
            # so a post-clear query must not resurrect the dead session's turns.
            await execute(
                "UPDATE core.sessions SET status = 'closed' WHERE id = $1", sid
            )
            result = await mgr.get_late_turns("telegram", "123", 1000.0)
            assert result == {"session_id": None, "turns": []}
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_get_late_turns_scoped_to_one_session(comms_db) -> None:
    async def scenario() -> None:
        mgr = await make_manager()
        try:
            sid_a = await _seed_session(surface="telegram", client_ref="aaa")
            sid_b = await _seed_session(surface="telegram", client_ref="bbb")
            await add_turn(sid_a, "user", "for-a", 1010.0)
            await add_turn(sid_b, "user", "for-b", 1010.0)
            result = await mgr.get_late_turns("telegram", "aaa", 1000.0)
            assert result["session_id"] == sid_a
            assert [t["content"] for t in result["turns"]] == ["for-a"]
        finally:
            await close_manager(mgr)

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


def test_fetch_session_memory_appends_continuation_no_duplication(comms_db) -> None:
    """The continuation is single-source: rendered as <pending-continuation>,
    never duplicated into the <session-memory> body."""
    from comms import bootstrap_loader as bl

    async def scenario() -> None:
        mgr = await make_manager()
        try:
            envelope = {
                "carry_forward": "## Summary\nDid stuff.",
                "continuation": [{"role": "user", "content": "pending ask"}],
            }
            await _seed_memory(json.dumps(envelope))

            out = await bl._fetch_session_memory(mind_id="ada", client_ref="cr")
            assert "<session-memory>" in out
            assert "Did stuff." in out
            assert "<pending-continuation>" in out
            assert "pending ask" in out
            # Single-source: "pending ask" appears exactly once.
            assert out.count("pending ask") == 1
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_fetch_session_memory_no_continuation_block_when_absent(comms_db) -> None:
    from comms import bootstrap_loader as bl

    async def scenario() -> None:
        mgr = await make_manager()
        try:
            envelope = {"carry_forward": "## Summary\nDid stuff."}
            await _seed_memory(json.dumps(envelope))
            out = await bl._fetch_session_memory(mind_id="ada", client_ref="cr")
            assert "<session-memory>" in out
            assert "<pending-continuation>" not in out
        finally:
            await close_manager(mgr)

    _run(scenario())
