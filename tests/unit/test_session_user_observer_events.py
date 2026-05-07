"""Tests for user-message observer events in SessionManager.send_message."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_send_message_publishes_user_event_to_observers_before_result():
    from core.sessions import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._db = AsyncMock()
    mgr._procs = {"sess-1": {"_mind_url": "http://mind"}}
    mgr._mind_ids = {}
    mgr._locks = {}
    mgr._registry = MagicMock()
    mgr.mind_registry = None  # Phase 3: _resolve_short_name uses this
    mgr._observer_queues = {}
    mgr._publish_session_event = AsyncMock()

    fake_session = {
        "mind_id": "nagatha",
        "model": "gpt-5",
        "autopilot": 0,
        "claude_sid": None,
        "summary": "Existing session",
    }
    mgr._get_row = AsyncMock(return_value=fake_session)
    mgr._spawn = AsyncMock()

    class _FakeContent:
        def __aiter__(self):
            self._lines = iter(
                [
                    b'data: {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}}\n',
                    b'data: {"type":"result","session_id":"thread-1","is_error":false}\n',
                ]
            )
            return self

        async def __anext__(self):
            try:
                return next(self._lines)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class _FakeResponse:
        status = 200
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse()

    with patch("aiohttp.ClientSession", _FakeClientSession):
        events = [event async for event in mgr.send_message("sess-1", "hello")]

    assert [event["type"] for event in events] == ["assistant", "result"]

    published = [call.args[1] for call in mgr._publish_session_event.await_args_list]
    assert published[0]["type"] == "user"
    assert published[0]["message"]["content"][0]["text"] == "hello"
