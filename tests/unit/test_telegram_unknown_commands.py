"""Unit tests for handle_unknown_command in telegram_bot."""

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


def _make_context(bot_username: str = "testbot"):
    """Create a mock context object."""
    context = MagicMock()
    context.bot.username = bot_username
    return context


def _make_lock(is_locked: bool = False):
    """Create a mock asyncio.Lock that works with async with."""
    lock = MagicMock()
    lock.locked = MagicMock(return_value=is_locked)
    lock.__aenter__ = AsyncMock(return_value=None)
    lock.__aexit__ = AsyncMock(return_value=None)
    return lock


@pytest.mark.asyncio
class TestHandleUnknownCommand:
    """Tests for the Telegram bot's handle_unknown_command function."""

    async def test_unknown_command_routes_to_stream(self) -> None:
        from clients.telegram_bot import handle_unknown_command

        update = _make_update("/remember buy milk")
        context = _make_context()

        mock_lock = _make_lock(is_locked=False)
        sent_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=sent_msg)

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.get_lock", return_value=mock_lock),
            patch(
                "clients.telegram_bot._stream_to_message",
                new_callable=AsyncMock,
                return_value=["Memory stored."],
            ) as mock_stream,
        ):
            await handle_unknown_command(update, context)

        mock_stream.assert_called_once()
        call_args = mock_stream.call_args
        # 4th positional arg is the prompt
        prompt = call_args[0][3]
        assert prompt == "/remember buy milk"

    async def test_unknown_command_strips_bot_mention(self) -> None:
        from clients.telegram_bot import handle_unknown_command

        update = _make_update("/remember@testbot something", chat_type="group")
        context = _make_context("testbot")

        mock_lock = _make_lock(is_locked=False)
        sent_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=sent_msg)

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.get_lock", return_value=mock_lock),
            patch(
                "clients.telegram_bot._stream_to_message",
                new_callable=AsyncMock,
                return_value=["Done."],
            ) as mock_stream,
        ):
            await handle_unknown_command(update, context)

        mock_stream.assert_called_once()
        call_args = mock_stream.call_args
        prompt = call_args[0][3]
        assert "@testbot" not in prompt
        assert prompt == "/remember something"

    async def test_unknown_command_auth_check_blocks_unauthorized(self) -> None:
        from clients.telegram_bot import handle_unknown_command

        update = _make_update("/remember something")
        context = _make_context()

        with patch("clients.telegram_bot._is_allowed_user", return_value=False):
            await handle_unknown_command(update, context)

        update.message.reply_text.assert_called_once_with("Not authorized.")

    async def test_unknown_command_applies_sanitize(self) -> None:
        from clients.telegram_bot import handle_unknown_command

        update = _make_update("/remember buy milk")
        context = _make_context()

        mock_lock = _make_lock(is_locked=False)
        sent_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=sent_msg)

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.get_lock", return_value=mock_lock),
            patch(
                "clients.telegram_bot._stream_to_message",
                new_callable=AsyncMock,
                return_value=["Done."],
            ),
        ):
            await handle_unknown_command(update, context)

        # The sent message should not contain raw JSON
        for call in update.effective_chat.send_message.call_args_list:
            assert "{" not in str(call)

    async def test_unknown_command_waits_when_locked(self) -> None:
        from clients.telegram_bot import handle_unknown_command

        update = _make_update("/remember something")
        context = _make_context()

        mock_lock = _make_lock(is_locked=True)

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot.get_lock", return_value=mock_lock),
        ):
            await handle_unknown_command(update, context)

        update.message.reply_text.assert_called_once_with(
            "Still processing your previous message, please wait."
        )
