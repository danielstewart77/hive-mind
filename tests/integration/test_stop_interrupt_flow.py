"""Integration tests for the /stop interrupt signal chain.

Covers: gateway session manager forwarding to the mind container,
recycling the live process, and reusing the saved thread on the next message.
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
        session_mgr._get_row = AsyncMock(return_value={"id": "sess-chain", "claude_sid": "conv-1"})
        session_mgr._kill_process = AsyncMock()

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
        assert result["resume_ready"] is True
        session_mgr._kill_process.assert_awaited_once_with("sess-chain")

    @pytest.mark.asyncio
    async def test_send_message_respawns_with_saved_thread_after_recycle(self, session_mgr):
        """If the live proc is gone but claude_sid exists, the next message respawns with resume."""
        from core.sessions import SessionManager

        class _AsyncBytesIter:
            def __init__(self, items):
                self._iter = iter(items)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

        session_mgr._db = AsyncMock()
        session_mgr._locks = {}
        session_mgr._procs = {}
        session_mgr._mind_ids = {}
        session_mgr._get_row = AsyncMock(return_value={
            "id": "sess-resume",
            "mind_id": "ada",
            "model": "sonnet",
            "autopilot": 0,
            "claude_sid": "conv-1",
            "summary": "Existing session",
        })

        async def fake_spawn(*args, **kwargs):
            session_mgr._procs["sess-resume"] = {"_mind_url": "http://mind-ada:8420"}

        session_mgr._spawn = AsyncMock(side_effect=fake_spawn)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.content = _AsyncBytesIter([
            b'data: {"type":"result","result":"ok","session_id":"conv-1"}\n'
        ])

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session_ctx)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_ctx.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=mock_session_ctx):
            events = [event async for event in SessionManager.send_message(session_mgr, "sess-resume", "hello")]

        session_mgr._spawn.assert_awaited_once()
        assert session_mgr._spawn.await_args.kwargs["resume_sid"] == "conv-1"
        assert events[-1]["type"] == "result"
