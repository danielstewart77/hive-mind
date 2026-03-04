"""Unit tests for _handle_server_command in telegram_bot."""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
class TestHandleServerCommand:
    """Tests for the Telegram bot's _handle_server_command function."""

    async def test_handle_server_command_fallback_no_json(self) -> None:
        from clients.telegram_bot import _handle_server_command

        mock_gateway = AsyncMock()
        mock_gateway.server_command = AsyncMock(return_value={"foo": "bar"})

        with patch("clients.telegram_bot.gateway", mock_gateway):
            result = await _handle_server_command("/unknown_cmd", 123, 456)

        assert "{" not in result
        assert "}" not in result
        assert result == "Done."

    async def test_handle_server_command_error_formats_message(self) -> None:
        from clients.telegram_bot import _handle_server_command

        mock_gateway = AsyncMock()
        mock_gateway.server_command = AsyncMock(
            return_value={"error": "something went wrong"}
        )

        with patch("clients.telegram_bot.gateway", mock_gateway):
            result = await _handle_server_command("/sessions", 123, 456)

        assert result.startswith("Error:")
        assert "something went wrong" in result

    async def test_handle_server_command_sessions_formats_list(self) -> None:
        from clients.telegram_bot import _handle_server_command

        mock_gateway = AsyncMock()
        mock_gateway.server_command = AsyncMock(
            return_value=[
                {
                    "id": "abcd1234-5678-9012-3456-789012345678",
                    "status": "running",
                    "summary": "Test session",
                    "model": "sonnet",
                    "last_active": 0,
                }
            ]
        )

        with patch("clients.telegram_bot.gateway", mock_gateway):
            result = await _handle_server_command("/sessions", 123, 456)

        assert "Sessions" in result or "session" in result.lower()
        # Must not be raw JSON
        assert not result.startswith("[")
        assert not result.startswith("{")

    async def test_handle_server_command_new_formats_short_id(self) -> None:
        from clients.telegram_bot import _handle_server_command

        mock_gateway = AsyncMock()
        mock_gateway.server_command = AsyncMock(
            return_value={"id": "abcd1234-full-uuid", "status": "running"}
        )

        with patch("clients.telegram_bot.gateway", mock_gateway):
            result = await _handle_server_command("/new", 123, 456)

        assert "abcd1234" in result
        assert "{" not in result
