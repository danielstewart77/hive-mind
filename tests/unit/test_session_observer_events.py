"""Unit tests for passive session observer fan-out."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestSessionObserverEvents:
    """Verify SessionManager observer subscriptions receive live events."""

    @pytest.mark.asyncio
    async def test_stream_session_events_yields_published_event(self):
        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        mgr._get_row = AsyncMock(return_value={"id": "sess-1", "status": "running"})

        async def consume_one():
            async for event in mgr.stream_session_events("sess-1"):
                return event

        task = asyncio.create_task(consume_one())
        await asyncio.sleep(0)
        await mgr._publish_session_event("sess-1", {"type": "assistant", "content": "hello"})

        event = await asyncio.wait_for(task, timeout=1)
        assert event == {"type": "assistant", "content": "hello"}

    @pytest.mark.asyncio
    async def test_stream_session_events_returns_closed_event_for_closed_session(self):
        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        mgr._get_row = AsyncMock(return_value={"id": "sess-2", "status": "closed"})

        events = []
        async for event in mgr.stream_session_events("sess-2"):
            events.append(event)

        assert events == [{"type": "session_closed", "session_id": "sess-2"}]
