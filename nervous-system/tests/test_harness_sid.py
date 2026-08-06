"""Durable provider-native thread identity for Codex sessions."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from unittest.mock import patch

from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


async def _manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    return mgr


def test_harness_sid_survives_manager_restart() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            now = time.time()
            await mgr._db.execute(
                """INSERT INTO sessions
                   (id, claude_sid, owner_type, owner_ref, model, created_at,
                    last_active, status, mind_id)
                   VALUES ('s1', 'gateway-conversation', 'web', 'u1', 'gpt',
                           ?, ?, 'idle', 'm1')""",
                (now, now),
            )
            await mgr._db.commit()
            await mgr.set_harness_sid("s1", "codex-thread-1")
            await mgr.shutdown()

            resumed = await _manager(tmp)
            try:
                row = await resumed.get_session("s1")
                assert row["claude_sid"] == "gateway-conversation"
                assert row["harness_sid"] == "codex-thread-1"
            finally:
                await resumed.shutdown()

    _run(scenario())


def test_empty_harness_sid_clears_a_failed_codex_thread() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                now = time.time()
                await mgr._db.execute(
                    """INSERT INTO sessions
                       (id, claude_sid, harness_sid, owner_type, owner_ref, model,
                        created_at, last_active, status, mind_id)
                       VALUES ('failed', 'gateway', 'dirty-thread', 'web', 'u1',
                               'gpt', ?, ?, 'idle', 'm1')""",
                    (now, now),
                )
                await mgr._db.commit()
                row = await mgr.set_harness_sid("failed", "")
                assert row["harness_sid"] is None
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_codex_thread_started_event_is_persisted() -> None:
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
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                now = time.time()
                await mgr._db.execute(
                    """INSERT INTO sessions
                       (id, claude_sid, owner_type, owner_ref, model, created_at,
                        last_active, status, mind_id)
                       VALUES ('s2', 'gateway-conversation', 'web', 'u1', 'gpt',
                               ?, ?, 'running', 'm1')""",
                    (now, now),
                )
                await mgr._db.commit()
                mgr._procs["s2"] = {"_mind_url": "http://mind.test"}
                mgr._mind_ids["s2"] = "m1"
                with patch("aiohttp.ClientSession", Client):
                    events = [event async for event in mgr.send_message("s2", "hello")]
                assert events == [{"type": "result", "session_id": "codex-thread-2", "is_error": False}]
                row = await mgr.get_session("s2")
                assert row["harness_sid"] == "codex-thread-2"
                mgr._procs.clear()
            finally:
                await mgr.shutdown()

    _run(scenario())
