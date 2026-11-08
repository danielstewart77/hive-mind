"""Mind error propagation — NS side.

A non-200, non-404 response from a mind server used to fall through to the
SSE line-reader, which found no `data:` lines in the JSON error body and
ended the stream silently — surfaces then showed a useless generic error.
send_message must instead yield a result event carrying the mind's actual
error detail.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from comms.sessions import SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    make_manager,
    scratch_dsn,
    seed_session,
)



async def _make_manager() -> SessionManager:
    mgr = await make_manager()
    return mgr


async def _seed_running_session(mgr: SessionManager) -> str:
    session_id = "sess-err"
    await seed_session(
        session_id, owner_type="telegram", owner_ref="123", model="opus",
        status="running", mind_id="ada", summary="existing chat",
    )
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
        mgr = await _make_manager()
        try:
            sid = await _seed_running_session(mgr)
            _FakeClientSession.response = _FakeResponse(status, body)
            with patch("aiohttp.ClientSession", _FakeClientSession):
                events = []
                async for event in mgr.send_message(sid, "hello"):
                    events.append(event)
                return events
        finally:
            await close_manager(mgr)

    return asyncio.run(scenario())


def test_500_from_mind_yields_error_result_event(comms_db) -> None:
    events = _run_error_case(500, '{"error": "Process not running"}')

    assert len(events) == 1
    event = events[0]
    assert event["type"] == "result"
    assert event["is_error"] is True
    assert "Process not running" in event["result"]
    assert "HTTP 500" in event["result"]
    assert "ada" in event["result"]


def test_non_json_error_body_passed_through_verbatim(comms_db) -> None:
    events = _run_error_case(502, "Bad Gateway")

    assert len(events) == 1
    assert events[0]["is_error"] is True
    assert "Bad Gateway" in events[0]["result"]


def test_empty_error_body_reports_http_status(comms_db) -> None:
    events = _run_error_case(503, "")

    assert len(events) == 1
    assert events[0]["is_error"] is True
    assert "HTTP 503" in events[0]["result"]
