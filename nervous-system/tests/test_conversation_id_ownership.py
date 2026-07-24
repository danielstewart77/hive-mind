"""Who owns a session's conversation id.

One place mints it: ``create_session``. From that instant the session has a
conversation, whether or not anything has spoken to it yet, and every
surface that opens the session is handed the same id. Minds pin their
harness to it and never mint their own; nothing downstream ever overwrites
it. Before that rule existed the id materialised only when a turn finished,
so a terminal attaching mid-first-turn was told the session had no
conversation and opened a blank one beside a live chat.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from comms.models import ModelRegistry, Provider
from comms.sessions import SessionManager


def _registry() -> ModelRegistry:
    return ModelRegistry({"anthropic": Provider(name="anthropic")}, {"opus": "anthropic"})


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(_registry())
    await mgr.start()
    return mgr


async def _seed(mgr: SessionManager, session_id: str = "sess-turn",
                claude_sid: str = "conv-pinned") -> str:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid, created_at,
                                 last_active, status, mind_id, summary)
           VALUES (?, 'telegram', '123', 'opus', ?, ?, ?, 'running', 'ada', 'new chat')""",
        (session_id, claude_sid, now, now),
    )
    await mgr._db.commit()
    mgr._procs[session_id] = {"_mind_url": "http://mind.test:8420"}
    mgr._mind_ids[session_id] = "ada"
    return session_id


class _SseResponse:
    """A 200 streaming response over a canned list of events."""

    def __init__(self, events, on_event=None):
        self.status = 200
        self._events = events
        self._on_event = on_event

    @property
    def content(self):
        return self._lines()

    async def _lines(self):
        for event in self._events:
            yield ("data: " + json.dumps(event) + "\n").encode()
            if self._on_event:
                await self._on_event(event)

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_session_class(response):
    class _FakeClientSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, **kwargs):
            return response

    return _FakeClientSession


def _drain(mgr, sid, response):
    async def go():
        with patch("aiohttp.ClientSession", _fake_session_class(response)):
            return [e async for e in mgr.send_message(sid, "hello")]

    return go()


async def _stored_sid(mgr: SessionManager, session_id: str) -> str | None:
    cur = await mgr._db.execute(
        "SELECT claude_sid FROM sessions WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    return row["claude_sid"] if row else None


def _patched_deps(mgr, spawned=None):
    """create_session's two outbound dependencies, stubbed."""

    async def fake_spawn(session_id, model, **kw):
        if spawned is not None:
            spawned.append({"session_id": session_id, **kw})
        mgr._procs[session_id] = {"_mind_url": "http://mind.test:8420"}

    async def fake_mind(db, mind_id):
        return {"name": "ada", "model": "opus", "gateway_url": "http://mind.test:8420"}

    async def fake_blocks(**kw):
        return ""

    mgr._spawn = fake_spawn  # type: ignore[assignment]
    return patch("comms.broker.get_mind_by_id", fake_mind), \
        patch("comms.bootstrap_loader.compose_prompt_blocks", fake_blocks)


def test_a_session_owns_a_conversation_from_the_moment_it_is_created() -> None:
    """No window in which "what conversation is this?" has no answer."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            spawned: list[dict] = []
            try:
                p_mind, p_blocks = _patched_deps(mgr, spawned)
                with p_mind, p_blocks:
                    session = await mgr.create_session(
                        owner_type="telegram", owner_ref="123", client_ref="123",
                        model="opus", mind_id="ada",
                    )

                sid = session["id"]
                stored = await _stored_sid(mgr, sid)
                assert stored, "conversation id minted at creation"
                # The mind is told the same id the row already holds.
                assert spawned[0]["resume_sid"] == stored
            finally:
                await mgr.shutdown()

    asyncio.run(scenario())


def test_two_sessions_get_two_conversations() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                p_mind, p_blocks = _patched_deps(mgr)
                with p_mind, p_blocks:
                    a = await mgr.create_session(owner_type="telegram", owner_ref="1",
                                                 client_ref="1", model="opus", mind_id="ada")
                    b = await mgr.create_session(owner_type="telegram", owner_ref="2",
                                                 client_ref="2", model="opus", mind_id="ada")

                sid_a = await _stored_sid(mgr, a["id"])
                sid_b = await _stored_sid(mgr, b["id"])
                assert sid_a and sid_b and sid_a != sid_b
            finally:
                await mgr.shutdown()

    asyncio.run(scenario())


def test_a_mind_reporting_a_different_conversation_does_not_move_the_session() -> None:
    """The harness echoes the id it was pinned to. It is not authority."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed(mgr, claude_sid="conv-pinned")
                response = _SseResponse([
                    {"type": "system", "subtype": "init", "session_id": "conv-invented"},
                    {"type": "assistant", "message": {"content": "hi"}},
                    {"type": "result", "session_id": "conv-invented"},
                ])
                await _drain(mgr, sid, response)

                assert await _stored_sid(mgr, sid) == "conv-pinned"
            finally:
                await mgr.shutdown()

    asyncio.run(scenario())


def test_the_id_a_terminal_is_handed_never_changes_mid_turn() -> None:
    """What a concurrent web-terminal attach would be told, event by event."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed(mgr, claude_sid="conv-stable")
                seen: list[str | None] = []

                async def after(event):
                    seen.append(await _stored_sid(mgr, sid))

                response = _SseResponse(
                    [
                        {"type": "system", "subtype": "init", "session_id": "conv-stable"},
                        {"type": "assistant", "message": {"content": "working"}},
                        {"type": "result", "session_id": "conv-stable"},
                    ],
                    on_event=after,
                )
                await _drain(mgr, sid, response)

                assert seen and set(seen) == {"conv-stable"}
            finally:
                await mgr.shutdown()

    asyncio.run(scenario())


def test_spawning_without_a_conversation_id_is_refused() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                with pytest.raises(ValueError, match="without a conversation id"):
                    await mgr._spawn("sess-x", "opus", mind_id="ada")
            finally:
                await mgr.shutdown()

    asyncio.run(scenario())


def test_legacy_rows_without_a_conversation_id_are_backfilled_on_start() -> None:
    """Rows created before the id was minted at creation never finished a
    turn, so there is no conversation on disk to lose — but the hole is
    exactly what an attach used to invent its way around."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            now = time.time()
            await mgr._db.execute(
                """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                         created_at, last_active, status, mind_id)
                   VALUES ('legacy', 'telegram', '9', 'opus', NULL, ?, ?, 'idle', 'ada')""",
                (now, now),
            )
            await mgr._db.commit()
            await mgr.shutdown()

            mgr = await _make_manager(tmp)
            try:
                assert await _stored_sid(mgr, "legacy")
            finally:
                await mgr.shutdown()

    asyncio.run(scenario())
