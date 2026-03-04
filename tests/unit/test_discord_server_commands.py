"""Unit tests for _handle_server_command in discord_bot."""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
class TestDiscordHandleServerCommand:
    """Tests for the Discord bot's _handle_server_command function."""

    async def test_discord_handle_server_command_fallback_no_json(self) -> None:
        from clients.discord_bot import _handle_server_command

        mock_gateway = AsyncMock()
        mock_gateway.server_command = AsyncMock(return_value={"foo": "bar"})

        with patch("clients.discord_bot.gateway", mock_gateway):
            result = await _handle_server_command("/unknown_cmd", 123, 456)

        assert "{" not in result
        assert "}" not in result
        assert result == "Done."

    async def test_discord_handle_server_command_error(self) -> None:
        from clients.discord_bot import _handle_server_command

        mock_gateway = AsyncMock()
        mock_gateway.server_command = AsyncMock(
            return_value={"error": "not found"}
        )

        with patch("clients.discord_bot.gateway", mock_gateway):
            result = await _handle_server_command("/sessions", 123, 456)

        assert result.startswith("Error:")
        assert "not found" in result

    async def test_discord_handle_server_command_new_formats_id(self) -> None:
        from clients.discord_bot import _handle_server_command

        mock_gateway = AsyncMock()
        mock_gateway.server_command = AsyncMock(
            return_value={"id": "abcd1234-full-uuid", "status": "running"}
        )

        with patch("clients.discord_bot.gateway", mock_gateway):
            result = await _handle_server_command("/new", 123, 456)

        assert "abcd1234" in result
        assert "{" not in result
