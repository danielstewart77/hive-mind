"""Unit tests for /stop command in telegram_bot.py.

Covers: interrupt on active session, no active session, nothing running,
queue bypass, and unauthorized user rejection.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(user_id: int = 123, chat_id: int = 456):
    """Create a mock Telegram Update for /stop."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture(autouse=True)
def _patch_config():
    """Patch config so telegram_allowed_users includes test user."""
    with patch("clients.telegram_bot.config") as mock_config:
        mock_config.telegram_allowed_users = {123}
        mock_config.server_port = 8420
        mock_config.hitl_internal_token = "test-token"
        yield mock_config


@pytest.fixture()
def mock_gateway():
    """Patch the module-level gateway with a mock GatewayClient."""
    mock_gw = AsyncMock()
    with patch("clients.telegram_bot.gateway", mock_gw):
        yield mock_gw


class TestStopCommand:
    """cmd_stop handler tests."""

    @pytest.mark.asyncio
    async def test_stop_calls_interrupt_on_active_session(self, mock_gateway):
        """/stop calls find_active_session then interrupt_session and replies 'Interrupted.'"""
        from clients.telegram_bot import cmd_stop

        mock_gateway.find_active_session = AsyncMock(return_value="sess-1")
        mock_gateway.interrupt_session = AsyncMock(
            return_value={"ok": True, "session_id": "sess-1"}
        )

        update = _make_update()
        await cmd_stop(update, MagicMock())

        mock_gateway.find_active_session.assert_called_once_with(123, 456)
        mock_gateway.interrupt_session.assert_called_once_with("sess-1")
        update.message.reply_text.assert_called_once_with("Interrupted.")

    @pytest.mark.asyncio
    async def test_stop_replies_no_active_session_when_not_found(self, mock_gateway):
        """When find_active_session returns None, bot replies 'No active session.' without calling interrupt."""
        from clients.telegram_bot import cmd_stop

        mock_gateway.find_active_session = AsyncMock(return_value=None)

        update = _make_update()
        await cmd_stop(update, MagicMock())

        update.message.reply_text.assert_called_once_with("No active session.")
        mock_gateway.interrupt_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_replies_nothing_running(self, mock_gateway):
        """When interrupt returns nothing_running message, bot replies 'Nothing running.'"""
        from clients.telegram_bot import cmd_stop

        mock_gateway.find_active_session = AsyncMock(return_value="sess-1")
        mock_gateway.interrupt_session = AsyncMock(
            return_value={"ok": True, "session_id": "sess-1", "message": "nothing_running"}
        )

        update = _make_update()
        await cmd_stop(update, MagicMock())

        update.message.reply_text.assert_called_once_with("Nothing running.")

    @pytest.mark.asyncio
    async def test_stop_bypasses_queue_when_lock_held(self, mock_gateway):
        """/stop does NOT add to the queue even when lock is held; calls interrupt directly."""
        from clients.telegram_bot import cmd_stop
        from core.gateway_client import get_lock, get_queue

        mock_gateway.find_active_session = AsyncMock(return_value="sess-1")
        mock_gateway.interrupt_session = AsyncMock(
            return_value={"ok": True, "session_id": "sess-1"}
        )

        update = _make_update()
        chat_id = 456
        lock = get_lock(chat_id)

        # Simulate lock being held by another coroutine
        await lock.acquire()
        try:
            await cmd_stop(update, MagicMock())

            # Verify interrupt was called (not queued)
            mock_gateway.interrupt_session.assert_called_once_with("sess-1")
            update.message.reply_text.assert_called_once_with("Interrupted.")

            # Verify nothing was added to the queue
            queue = get_queue(chat_id)
            assert queue.empty()
        finally:
            lock.release()

    @pytest.mark.asyncio
    async def test_stop_unauthorized_user_rejected(self, mock_gateway):
        """An unauthorized user gets 'Not authorized.' reply."""
        from clients.telegram_bot import cmd_stop

        update = _make_update(user_id=999)  # Not in allowed users
        await cmd_stop(update, MagicMock())

        update.message.reply_text.assert_called_once_with("Not authorized.")
        mock_gateway.interrupt_session.assert_not_called()
