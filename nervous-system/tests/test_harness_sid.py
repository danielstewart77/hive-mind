"""Durable provider-native thread identity for Codex sessions."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from comms.sessions import SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    make_manager,
    scratch_dsn,
    seed_session,
)


def _run(coro):
    return asyncio.run(coro)


async def _manager() -> SessionManager:
    mgr = await make_manager()
    return mgr


def test_harness_sid_survives_manager_restart(comms_db) -> None:
    async def scenario() -> None:
        mgr = await _manager()
        await seed_session(
            "s1", claude_sid="gateway-conversation", owner_type="web",
            owner_ref="u1", model="gpt", status="idle", mind_id="m1",
        )
        await mgr.set_harness_sid("s1", "codex-thread-1")
        await close_manager(mgr)

        resumed = await _manager()
        try:
            row = await resumed.get_session("s1")
            assert row["claude_sid"] == "gateway-conversation"
            assert row["harness_sid"] == "codex-thread-1"
        finally:
            await close_manager(resumed)

    _run(scenario())


def test_empty_harness_sid_clears_a_failed_codex_thread(comms_db) -> None:
    async def scenario() -> None:
        mgr = await _manager()
        try:
            await seed_session(
                "failed", claude_sid="gateway", harness_sid="dirty-thread",
                owner_type="web", owner_ref="u1", model="gpt", status="idle",
                mind_id="m1",
            )
            row = await mgr.set_harness_sid("failed", "")
            assert row["harness_sid"] is None
        finally:
            await close_manager(mgr)

    _run(scenario())


def test_codex_thread_started_event_is_persisted(comms_db) -> None:
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def text(self):
            return ""

        @property
        def content(self):
            async def lines():
                events = [
                    {"type": "codex_event", "event": {
                        "type": "thread.started", "thread_id": "codex-thread-2"
                    }, "_observer_only": True},
                    {"type": "result", "session_id": "codex-thread-2", "is_error": False},
                ]
                for event in events:
                    yield ("data: " + json.dumps(event) + "\n").encode()
            return lines()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            return Response()

    async def scenario() -> None:
        mgr = await _manager()
        try:
            await seed_session(
                "s2", claude_sid="gateway-conversation", owner_type="web",
                owner_ref="u1", model="gpt", status="running", mind_id="m1",
            )
            mgr._procs["s2"] = {"_mind_url": "http://mind.test"}
            mgr._mind_ids["s2"] = "m1"
            with patch("aiohttp.ClientSession", Client):
                events = [event async for event in mgr.send_message("s2", "hello")]
            assert events == [{"type": "result", "session_id": "codex-thread-2", "is_error": False}]
            row = await mgr.get_session("s2")
            assert row["harness_sid"] == "codex-thread-2"
            mgr._procs.clear()
        finally:
            await close_manager(mgr)

    _run(scenario())
