"""Integration tests for the /stop interrupt signal chain.

Covers: gateway session manager forwarding to mind container,
and verifying session status is not changed by interrupt.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture()
def session_mgr():
    """Create a SessionManager with mocked dependencies."""
    with patch("core.sessions.config") as mock_config, \
         patch("core.sessions.aiosqlite"):
        mock_config.default_model = "sonnet"
        mock_config.idle_timeout_minutes = 30

        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        return mgr


class TestInterruptSignalChain:
    """End-to-end interrupt signal chain tests."""

    @pytest.mark.asyncio
    async def test_interrupt_signal_chain(self, session_mgr):
        """Gateway session manager forwards POST to the correct mind container URL."""
        mind_url = "http://mind-ada:8420"
        session_mgr._procs["sess-chain"] = {"_mind_url": mind_url}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True, "session_id": "sess-chain"})

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session_ctx)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_ctx.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            result = await session_mgr.interrupt_session("sess-chain")

        # Verify the correct URL was called
        call_args = mock_session_ctx.post.call_args
        assert call_args[0][0] == f"{mind_url}/sessions/sess-chain/interrupt"

        # Verify the response is passed through
        assert result["ok"] is True
        assert result["session_id"] == "sess-chain"

    @pytest.mark.asyncio
    async def test_interrupt_does_not_change_session_status(self, session_mgr):
        """After interrupt_session(), the session remains in _procs (not removed)."""
        mind_url = "http://mind-ada:8420"
        session_mgr._procs["sess-alive"] = {"_mind_url": mind_url}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True, "session_id": "sess-alive"})

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session_ctx)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_ctx.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            await session_mgr.interrupt_session("sess-alive")

        # Session is still tracked (not removed from _procs)
        assert "sess-alive" in session_mgr._procs
        # The proc info is unchanged
        assert session_mgr._procs["sess-alive"]["_mind_url"] == mind_url
