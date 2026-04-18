"""Unit tests for SessionManager.interrupt_session().

Covers: forwarding interrupt to mind container, session not found,
missing mind_url, and mind container unreachable.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture()
def session_mgr():
    """Create a minimal SessionManager with mocked dependencies."""
    with patch("core.sessions.config") as mock_config, \
         patch("core.sessions.aiosqlite"):
        mock_config.default_model = "sonnet"
        mock_config.idle_timeout_minutes = 30

        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        return mgr


class TestInterruptSession:
    """SessionManager.interrupt_session() tests."""

    @pytest.mark.asyncio
    async def test_interrupt_session_forwards_to_mind_container(self, session_mgr):
        """interrupt_session() POSTs to {mind_url}/sessions/{id}/interrupt and returns the response."""
        session_mgr._procs["sess-1"] = {"_mind_url": "http://mind-ada:8420"}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True, "session_id": "sess-1"})

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session_ctx)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_ctx.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            result = await session_mgr.interrupt_session("sess-1")

        assert result == {"ok": True, "session_id": "sess-1"}
        mock_session_ctx.post.assert_called_once()
        call_url = mock_session_ctx.post.call_args[0][0]
        assert call_url == "http://mind-ada:8420/sessions/sess-1/interrupt"

    @pytest.mark.asyncio
    async def test_interrupt_session_not_found_raises_lookup_error(self, session_mgr):
        """interrupt_session() raises LookupError when session_id not in _procs."""
        with pytest.raises(LookupError, match="Session not found"):
            await session_mgr.interrupt_session("nonexistent")

    @pytest.mark.asyncio
    async def test_interrupt_session_no_mind_url_raises_value_error(self, session_mgr):
        """interrupt_session() raises ValueError when _procs entry has no _mind_url."""
        session_mgr._procs["sess-no-url"] = {}

        with pytest.raises(ValueError, match="No mind container URL"):
            await session_mgr.interrupt_session("sess-no-url")

    @pytest.mark.asyncio
    async def test_interrupt_session_mind_container_unreachable(self, session_mgr):
        """interrupt_session() raises RuntimeError when the mind container HTTP call fails."""
        import aiohttp
        session_mgr._procs["sess-down"] = {"_mind_url": "http://mind-ada:8420"}

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session_ctx)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_ctx.post = MagicMock(side_effect=aiohttp.ClientError("connection refused"))

        with patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            with pytest.raises(RuntimeError, match="Mind container unreachable"):
                await session_mgr.interrupt_session("sess-down")
