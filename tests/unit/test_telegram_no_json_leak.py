"""Comprehensive tests verifying no JSON leaks to the user in any Telegram code path."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_update(text: str, user_id: int = 123, chat_id: int = 456, chat_type: str = "private"):
    """Create a mock Update object for testing."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat.send_message = AsyncMock()
    return update


def _make_context(bot_username: str = "testbot", args: list | None = None):
    """Create a mock context object."""
    context = MagicMock()
    context.bot.username = bot_username
    context.args = args or []
    return context


def _make_lock(is_locked: bool = False):
    """Create a mock asyncio.Lock."""
    lock = MagicMock()
    lock.locked = MagicMock(return_value=is_locked)
    lock.__aenter__ = AsyncMock(return_value=None)
    lock.__aexit__ = AsyncMock(return_value=None)
    return lock


@pytest.mark.asyncio
class TestNoJsonLeak:
    """End-to-end tests verifying no raw JSON reaches the user."""

    async def test_server_command_new_returns_readable(self) -> None:
        from clients.telegram_bot import cmd_new

        update = _make_update("/new")
        context = _make_context()

        mock_gateway = AsyncMock()
        mock_gateway.server_command = AsyncMock(
            return_value={"id": "abcd1234-5678-9012-3456-789012345678", "status": "running"}
        )

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.gateway", mock_gateway),
        ):
            await cmd_new(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "{" not in reply_text
        assert "abcd1234" in reply_text

    async def test_server_command_clear_returns_readable(self) -> None:
        from clients.telegram_bot import cmd_clear

        update = _make_update("/clear")
        context = _make_context()

        mock_gateway = AsyncMock()
        mock_gateway.server_command = AsyncMock(
            return_value={"id": "efgh5678-1234-5678-9012-345678901234", "status": "running"}
        )

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.gateway", mock_gateway),
        ):
            await cmd_clear(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "{" not in reply_text
        assert "efgh5678" in reply_text

    async def test_skill_command_json_result_sanitized(self) -> None:
        from clients.telegram_bot import cmd_skill

        update = _make_update("/skill remember")
        context = _make_context(args=["remember"])

        mock_lock = _make_lock(is_locked=False)
        sent_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=sent_msg)

        async def mock_query_stream(*args, **kwargs):
            yield '{"status": "completed", "session_id": "abc"}'

        mock_gateway = MagicMock()
        mock_gateway.query_stream = mock_query_stream

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.gateway", mock_gateway),
            patch("clients.telegram_bot.get_lock", return_value=mock_lock),
        ):
            await cmd_skill(update, context)

        # Check the final edit_text call
        final_text = sent_msg.edit_text.call_args[0][0]
        assert "{" not in final_text
        assert final_text == "Done."

    async def test_unknown_slash_command_json_result_sanitized(self) -> None:
        from clients.telegram_bot import handle_unknown_command

        update = _make_update("/remember buy milk")
        context = _make_context()

        mock_lock = _make_lock(is_locked=False)
        sent_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=sent_msg)

        async def mock_query_stream(*args, **kwargs):
            yield '{"status": "completed"}'

        mock_gateway = MagicMock()
        mock_gateway.query_stream = mock_query_stream

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.gateway", mock_gateway),
            patch("clients.telegram_bot.get_lock", return_value=mock_lock),
        ):
            await handle_unknown_command(update, context)

        final_text = sent_msg.edit_text.call_args[0][0]
        assert "{" not in final_text
        assert final_text == "Done."

    async def test_regular_text_json_result_sanitized(self) -> None:
        from clients.telegram_bot import handle_text

        update = _make_update("tell me about something")
        context = _make_context()

        mock_lock = _make_lock(is_locked=False)
        sent_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=sent_msg)

        async def mock_query_stream(*args, **kwargs):
            yield '{"result": "data"}'

        mock_gateway = MagicMock()
        mock_gateway.query_stream = mock_query_stream

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.gateway", mock_gateway),
            patch("clients.telegram_bot.get_lock", return_value=mock_lock),
        ):
            await handle_text(update, context)

        final_text = sent_msg.edit_text.call_args[0][0]
        assert "{" not in final_text
        assert final_text == "Done."

    async def test_session_completion_payload_never_leaks(self) -> None:
        """The exact payload from the bug report must not leak."""
        from clients.telegram_bot import handle_unknown_command

        update = _make_update("/plan weekly")
        context = _make_context()

        mock_lock = _make_lock(is_locked=False)
        sent_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=sent_msg)

        async def mock_query_stream(*args, **kwargs):
            yield '{"status": "completed", "session_id": "7af0b1c0-f768-45c8-83c0-df40f4a27532"}'

        mock_gateway = MagicMock()
        mock_gateway.query_stream = mock_query_stream

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.gateway", mock_gateway),
            patch("clients.telegram_bot.get_lock", return_value=mock_lock),
        ):
            await handle_unknown_command(update, context)

        final_text = sent_msg.edit_text.call_args[0][0]
        assert final_text == "Done."
        assert "{" not in final_text
        assert "session_id" not in final_text
