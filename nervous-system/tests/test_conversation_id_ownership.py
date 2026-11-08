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
from unittest.mock import patch

import pytest

from comms.sessions import SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    fetchval,
    make_manager,
    register_mind,
    scratch_dsn,
    seed_session,
)



async def _make_manager() -> SessionManager:
    mgr = await make_manager()
    return mgr


async def _seed(mgr: SessionManager, session_id: str = "sess-turn",
                claude_sid: str = "conv-pinned") -> str:
    await seed_session(
        session_id, owner_type="telegram", owner_ref="123", model="opus",
        claude_sid=claude_sid, status="running", mind_id="ada", summary="new chat",
    )
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
    return await fetchval(
        "SELECT conversation_id FROM core.sessions WHERE id = $1", session_id
    )


async def _patched_deps(mgr, spawned=None):
    """create_session's outbound dependency stubbed, and its mind registered.

    The mind declares no harness, so no harness listing is asked of it.
    """

    async def fake_spawn(session_id, model, **kw):
        if spawned is not None:
            spawned.append({"session_id": session_id, **kw})
        mgr._procs[session_id] = {"_mind_url": "http://mind.test:8420"}

    async def fake_blocks(**kw):
        return ""

    mgr._spawn = fake_spawn  # type: ignore[assignment]
    await register_mind("ada", model="opus", harness="",
                        gateway_url="http://mind.test:8420")
    return patch("comms.bootstrap_loader.compose_prompt_blocks", fake_blocks)


def test_a_session_owns_a_conversation_from_the_moment_it_is_created(comms_db) -> None:
    """No window in which "what conversation is this?" has no answer."""

    async def scenario() -> None:
        mgr = await _make_manager()
        spawned: list[dict] = []
        try:
            p_blocks = await _patched_deps(mgr, spawned)
            with p_blocks:
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
            await close_manager(mgr)

    asyncio.run(scenario())


def test_two_sessions_get_two_conversations(comms_db) -> None:
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            p_blocks = await _patched_deps(mgr)
            with p_blocks:
                a = await mgr.create_session(owner_type="telegram", owner_ref="1",
                                             client_ref="1", model="opus", mind_id="ada")
                b = await mgr.create_session(owner_type="telegram", owner_ref="2",
                                             client_ref="2", model="opus", mind_id="ada")

            sid_a = await _stored_sid(mgr, a["id"])
            sid_b = await _stored_sid(mgr, b["id"])
            assert sid_a and sid_b and sid_a != sid_b
        finally:
            await close_manager(mgr)

    asyncio.run(scenario())


def test_a_mind_reporting_a_different_conversation_does_not_move_the_session(comms_db) -> None:
    """The harness echoes the id it was pinned to. It is not authority."""

    async def scenario() -> None:
        mgr = await _make_manager()
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
            await close_manager(mgr)

    asyncio.run(scenario())


def test_the_id_a_terminal_is_handed_never_changes_mid_turn(comms_db) -> None:
    """What a concurrent web-terminal attach would be told, event by event."""

    async def scenario() -> None:
        mgr = await _make_manager()
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
            await close_manager(mgr)

    asyncio.run(scenario())


def test_spawning_without_a_conversation_id_is_refused(comms_db) -> None:
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            with pytest.raises(ValueError, match="without a conversation id"):
                await mgr._spawn("sess-x", "opus", mind_id="ada")
        finally:
            await close_manager(mgr)

    asyncio.run(scenario())


def test_legacy_rows_without_a_conversation_id_are_backfilled_on_start(comms_db) -> None:
    """Rows created before the id was minted at creation never finished a
    turn, so there is no conversation on disk to lose — but the hole is
    exactly what an attach used to invent its way around."""

    async def scenario() -> None:
        mgr = await _make_manager()
        await seed_session(
            "legacy", owner_type="telegram", owner_ref="9", model="opus",
            claude_sid=None, status="idle", mind_id="ada",
        )
        await close_manager(mgr)

        mgr = await _make_manager()
        try:
            assert await _stored_sid(mgr, "legacy")
        finally:
            await close_manager(mgr)

    asyncio.run(scenario())


def test_a_session_goes_out_under_both_the_old_and_the_new_names(comms_db) -> None:
    """Consumers move to the schema's names one at a time, so for now every
    session read carries both and they agree."""
    async def scenario():
        mgr = await _make_manager()
        try:
            await _seed(mgr, "sess-names", claude_sid="conv-both")
            session = await mgr.get_session("sess-names")
        finally:
            await close_manager(mgr)
        return session

    session = asyncio.run(scenario())

    assert session["claude_sid"] == session["conversation_id"] == "conv-both"
    assert session["owner_type"] == session["surface"] == "telegram"
    assert session["owner_ref"] == session["surface_id"] == "123"
    assert session["summary"] == session["opening_line"] == "new chat"
