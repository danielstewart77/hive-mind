"""Unit tests for _stream_to_message JSON sanitization in telegram_bot."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestStreamToMessageSanitization:
    """Tests for JSON sanitization in _stream_to_message."""

    async def test_stream_to_message_sanitizes_json_response(self) -> None:
        from clients.telegram_bot import _stream_to_message

        sent = AsyncMock()
        sent.edit_text = AsyncMock()

        async def mock_query_stream(*args, **kwargs):
            yield '{"status": "completed", "session_id": "abc"}'

        mock_gateway = MagicMock()
        mock_gateway.query_stream = mock_query_stream

        with patch("clients.telegram_bot.gateway", mock_gateway):
            final_chunks = await _stream_to_message(sent, 123, 456, "test prompt")

        # Final text must not contain raw JSON
        for chunk in final_chunks:
            assert "{" not in chunk
            assert "}" not in chunk
        assert final_chunks[0] == "Done."

    async def test_stream_to_message_preserves_normal_text(self) -> None:
        from clients.telegram_bot import _stream_to_message

        sent = AsyncMock()
        sent.edit_text = AsyncMock()

        async def mock_query_stream(*args, **kwargs):
            yield "Hello, here is your answer."

        mock_gateway = MagicMock()
        mock_gateway.query_stream = mock_query_stream

        with patch("clients.telegram_bot.gateway", mock_gateway):
            final_chunks = await _stream_to_message(sent, 123, 456, "test prompt")

        assert final_chunks[0] == "Hello, here is your answer."

    async def test_stream_to_message_sanitizes_only_final_not_preview(self) -> None:
        """During streaming, preview edits are allowed to contain anything.
        Only the final result is sanitized."""
        from clients.telegram_bot import _stream_to_message

        sent = AsyncMock()
        edit_calls = []

        async def track_edit_text(text):
            edit_calls.append(text)

        sent.edit_text = track_edit_text

        async def mock_query_stream(*args, **kwargs):
            yield '{"status": "completed"}'

        mock_gateway = MagicMock()
        mock_gateway.query_stream = mock_query_stream

        with (
            patch("clients.telegram_bot.gateway", mock_gateway),
            patch("time.monotonic", side_effect=[0.0, 3.0, 6.0]),
        ):
            final_chunks = await _stream_to_message(sent, 123, 456, "test", edit_interval=2.0)

        # Final result must be sanitized
        assert final_chunks[0] == "Done."

    async def test_stream_to_message_no_response_not_sanitized(self) -> None:
        """(No response) should pass through without sanitization."""
        from clients.telegram_bot import _stream_to_message

        sent = AsyncMock()
        sent.edit_text = AsyncMock()

        async def mock_query_stream(*args, **kwargs):
            return
            yield  # Make it an async generator

        mock_gateway = MagicMock()
        mock_gateway.query_stream = mock_query_stream

        with patch("clients.telegram_bot.gateway", mock_gateway):
            final_chunks = await _stream_to_message(sent, 123, 456, "test prompt")

        assert final_chunks[0] == "(No response)"
