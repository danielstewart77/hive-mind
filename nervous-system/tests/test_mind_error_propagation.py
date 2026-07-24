"""Mind error propagation — NS side.

A non-200, non-404 response from a mind server used to fall through to the
SSE line-reader, which found no `data:` lines in the JSON error body and
ended the stream silently — surfaces then showed a useless generic error.
send_message must instead yield a result event carrying the mind's actual
error detail.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from unittest.mock import patch

from comms.models import ModelRegistry, Provider
from comms.sessions import SessionManager


def _registry() -> ModelRegistry:
    return ModelRegistry({"anthropic": Provider(name="anthropic")}, {"opus": "anthropic"})


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(_registry())
    await mgr.start()
    return mgr


async def _seed_running_session(mgr: SessionManager) -> str:
    session_id = "sess-err"
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active,
                                 status, mind_id, summary)
           VALUES (?, 'telegram', '123', 'opus', ?, ?, 'running', 'ada', 'existing chat')""",
        (session_id, now, now),
    )
    await mgr._db.commit()
    # Pretend the mind process is already routed; skips the respawn path.
    mgr._procs[session_id] = {"_mind_url": "http://mind.test:8420"}
    mgr._mind_ids[session_id] = "ada"
    return session_id


class _FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeClientSession:
    """Stands in for aiohttp.ClientSession; returns a canned mind response."""

    response: _FakeResponse = None  # set per-test

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        return type(self).response


def _run_error_case(status: int, body: str) -> list[dict]:
    async def scenario() -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_running_session(mgr)
                _FakeClientSession.response = _FakeResponse(status, body)
                with patch("aiohttp.ClientSession", _FakeClientSession):
                    events = []
                    async for event in mgr.send_message(sid, "hello"):
                        events.append(event)
                    return events
            finally:
                await mgr.shutdown()

    return asyncio.run(scenario())


def test_500_from_mind_yields_error_result_event() -> None:
    events = _run_error_case(500, '{"error": "Process not running"}')

    assert len(events) == 1
    event = events[0]
    assert event["type"] == "result"
    assert event["is_error"] is True
    assert "Process not running" in event["result"]
    assert "HTTP 500" in event["result"]
    assert "ada" in event["result"]


def test_non_json_error_body_passed_through_verbatim() -> None:
    events = _run_error_case(502, "Bad Gateway")

    assert len(events) == 1
    assert events[0]["is_error"] is True
    assert "Bad Gateway" in events[0]["result"]


def test_empty_error_body_reports_http_status() -> None:
    events = _run_error_case(503, "")

    assert len(events) == 1
    assert events[0]["is_error"] is True
    assert "HTTP 503" in events[0]["result"]
