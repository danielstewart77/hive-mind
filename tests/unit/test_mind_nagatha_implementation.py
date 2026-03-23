"""Tests for minds/nagatha/implementation.py — SDK-based spawn/send/kill."""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestNagathaModuleInterface:
    """Verify Nagatha implementation exposes required functions."""

    def test_nagatha_module_has_spawn_function(self):
        from minds.nagatha.implementation import spawn
        assert callable(spawn)
        assert asyncio.iscoroutinefunction(spawn)

    def test_nagatha_module_has_send_function(self):
        from minds.nagatha.implementation import send
        assert callable(send)
        assert inspect.isasyncgenfunction(send)

    def test_nagatha_module_has_kill_function(self):
        from minds.nagatha.implementation import kill
        assert callable(kill)
        assert asyncio.iscoroutinefunction(kill)


class TestNagathaSpawn:
    """Verify Nagatha spawn creates an SDK client."""

    @pytest.mark.asyncio
    async def test_nagatha_spawn_creates_client(self):
        from minds.nagatha import implementation as nagatha_impl

        # Clear any leftover state
        nagatha_impl._sessions.clear()

        mock_build_prompt = MagicMock(return_value="test system prompt")

        await nagatha_impl.spawn(
            session_id="test-sdk-123",
            model="sonnet",
            build_base_prompt=mock_build_prompt,
        )

        assert "test-sdk-123" in nagatha_impl._sessions
        state = nagatha_impl._sessions["test-sdk-123"]
        assert state["client"] is not None
        assert state["system_prompt"] == "test system prompt"
        assert state["messages"] == []

        # Cleanup
        nagatha_impl._sessions.clear()


class TestNagathaSend:
    """Verify Nagatha send streams responses."""

    @pytest.mark.asyncio
    async def test_nagatha_send_streams_response(self):
        from minds.nagatha import implementation as nagatha_impl

        # Set up a mock streaming response
        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        # Create async iterator for text_stream
        async def mock_text_stream():
            yield "Hello "
            yield "world"

        mock_stream.text_stream = mock_text_stream()

        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=mock_stream)

        nagatha_impl._sessions["test-send"] = {
            "client": mock_client,
            "model": "claude-sonnet-4-20250514",
            "system_prompt": "test",
            "messages": [],
        }

        events = []
        async for event in nagatha_impl.send("test-send", "Hello"):
            events.append(event)

        # Should have assistant events + final result
        assert len(events) >= 2
        assert any(e["type"] == "assistant" for e in events)

        # Cleanup
        nagatha_impl._sessions.clear()

    @pytest.mark.asyncio
    async def test_nagatha_send_returns_result_event(self):
        from minds.nagatha import implementation as nagatha_impl

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        async def mock_text_stream():
            yield "Response"

        mock_stream.text_stream = mock_text_stream()

        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=mock_stream)

        nagatha_impl._sessions["test-result"] = {
            "client": mock_client,
            "model": "claude-sonnet-4-20250514",
            "system_prompt": "test",
            "messages": [],
        }

        events = []
        async for event in nagatha_impl.send("test-result", "Hello"):
            events.append(event)

        # Last event should be a result
        assert events[-1]["type"] == "result"
        assert events[-1]["result"] == "Response"

        nagatha_impl._sessions.clear()


class TestNagathaErrorSanitization:
    """Verify Nagatha send() does not expose raw exception details."""

    @pytest.mark.asyncio
    async def test_nagatha_send_error_does_not_expose_raw_exception(self):
        """M1: Raw str(e) must not appear in error responses -- information leakage."""
        from minds.nagatha import implementation as nagatha_impl

        # Set up a client whose stream() raises with sensitive details
        sensitive_msg = "Connection refused: https://api.anthropic.com/v1?key=sk-secret-key-123"
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(
            side_effect=RuntimeError(sensitive_msg)
        )

        nagatha_impl._sessions["test-error-sanitize"] = {
            "client": mock_client,
            "model": "claude-sonnet-4-20250514",
            "system_prompt": "test",
            "messages": [],
        }

        events = []
        async for event in nagatha_impl.send("test-error-sanitize", "Hello"):
            events.append(event)

        # Should get an error result
        assert len(events) == 1
        error_event = events[0]
        assert error_event["type"] == "result"
        assert error_event["is_error"] is True

        # The raw exception message must NOT appear in the errors list
        for err_str in error_event["errors"]:
            assert sensitive_msg not in err_str
            assert "sk-secret-key-123" not in err_str

        # Should contain a generic message instead
        assert any("SDK communication error" in e for e in error_event["errors"])

        nagatha_impl._sessions.clear()


class TestNagathaKill:
    """Verify Nagatha kill clears session state."""

    @pytest.mark.asyncio
    async def test_nagatha_kill_clears_state(self):
        from minds.nagatha import implementation as nagatha_impl

        nagatha_impl._sessions["test-kill"] = {
            "client": MagicMock(),
            "model": "test",
            "system_prompt": "",
            "messages": [],
        }

        await nagatha_impl.kill("test-kill")
        assert "test-kill" not in nagatha_impl._sessions
