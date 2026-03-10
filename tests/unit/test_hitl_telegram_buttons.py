"""Unit tests for HITL Telegram inline keyboard button support in server.py.

Covers:
- Step 2: Sending inline keyboard markup and tracking message IDs
- Step 3: Cleanup loop editing expired messages
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Step 2: Inline keyboard in _send_telegram_approval_request
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSendApprovalRequestInlineKeyboard:
    """Tests for _send_telegram_approval_request sending InlineKeyboardMarkup."""

    async def test_send_approval_request_includes_inline_keyboard(self) -> None:
        """POST body must include reply_markup with two rows, one button each."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"result": {"message_id": 42}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("server.aiohttp.ClientSession", return_value=mock_session), \
             patch("server._get_telegram_token", return_value="bot123"), \
             patch("server.config") as mock_cfg:
            mock_cfg.telegram_owner_chat_id = 999

            from server import _send_telegram_approval_request, _hitl_messages
            _hitl_messages.clear()

            await _send_telegram_approval_request("tok123", "Please approve this action")

        # Extract the JSON body passed to session.post
        call_kwargs = mock_session.post.call_args
        body = call_kwargs[1].get("json") if call_kwargs[1] else call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
        if body is None and "json" in (call_kwargs[1] or {}):
            body = call_kwargs[1]["json"]

        assert "reply_markup" in body
        keyboard = body["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 2  # two rows
        assert len(keyboard[0]) == 1  # one button per row
        assert len(keyboard[1]) == 1

        # Check button labels contain expected text
        assert "Approve" in keyboard[0][0]["text"]
        assert "Reject" in keyboard[1][0]["text"]

        # Check callback_data
        assert keyboard[0][0]["callback_data"] == "hitl_approve_tok123"
        assert keyboard[1][0]["callback_data"] == "hitl_deny_tok123"

    async def test_send_approval_request_message_format(self) -> None:
        """Message text should contain the approval header and full summary."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"result": {"message_id": 1}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        long_summary = "A" * 3500

        with patch("server.aiohttp.ClientSession", return_value=mock_session), \
             patch("server._get_telegram_token", return_value="bot123"), \
             patch("server.config") as mock_cfg:
            mock_cfg.telegram_owner_chat_id = 999

            from server import _send_telegram_approval_request, _hitl_messages
            _hitl_messages.clear()

            await _send_telegram_approval_request("tok456", long_summary)

        call_kwargs = mock_session.post.call_args
        body = call_kwargs[1].get("json") or call_kwargs[0][1]
        text = body["text"]

        # Should contain the approval header
        assert "Approval" in text
        # Should not truncate the summary (it's under 4000 chars)
        assert long_summary in text
        # Old /approve_ and /deny_ commands should NOT be present
        assert "/approve_" not in text
        assert "/deny_" not in text

    async def test_send_approval_request_tracks_message_id(self) -> None:
        """After successful send, _hitl_messages should contain (chat_id, message_id, text)."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"result": {"message_id": 42}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("server.aiohttp.ClientSession", return_value=mock_session), \
             patch("server._get_telegram_token", return_value="bot123"), \
             patch("server.config") as mock_cfg:
            mock_cfg.telegram_owner_chat_id = 999

            from server import _send_telegram_approval_request, _hitl_messages
            _hitl_messages.clear()

            await _send_telegram_approval_request("tok789", "test summary")

        assert "tok789" in _hitl_messages
        chat_id, message_id, orig_text = _hitl_messages["tok789"]
        assert chat_id == 999
        assert message_id == 42
        assert isinstance(orig_text, str)

    async def test_send_approval_request_handles_api_failure_gracefully(self) -> None:
        """On Telegram API failure, no crash and token not tracked."""
        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=Exception("network error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("server.aiohttp.ClientSession", return_value=mock_session), \
             patch("server._get_telegram_token", return_value="bot123"), \
             patch("server.config") as mock_cfg:
            mock_cfg.telegram_owner_chat_id = 999

            from server import _send_telegram_approval_request, _hitl_messages
            _hitl_messages.clear()

            # Should not raise
            await _send_telegram_approval_request("tokfail", "test")

        assert "tokfail" not in _hitl_messages

    async def test_send_approval_request_no_bot_token_skips(self) -> None:
        """When bot token is None, function should return without error."""
        with patch("server._get_telegram_token", return_value=None), \
             patch("server.config") as mock_cfg:
            mock_cfg.telegram_owner_chat_id = 999

            from server import _send_telegram_approval_request, _hitl_messages
            _hitl_messages.clear()

            # Should not raise
            await _send_telegram_approval_request("tokskip", "test")

        assert "tokskip" not in _hitl_messages


# ---------------------------------------------------------------------------
# Step 3: Cleanup loop editing expired messages
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCleanupLoopEditExpiredMessages:
    """Tests for _edit_hitl_message and cleanup loop integration."""

    async def test_edit_hitl_message_calls_telegram_api(self) -> None:
        """_edit_hitl_message should call editMessageText with reply_markup in a single call."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("server.aiohttp.ClientSession", return_value=mock_session), \
             patch("server._get_telegram_token", return_value="bot123"), \
             patch("server.config") as mock_cfg:
            mock_cfg.telegram_owner_chat_id = 999

            from server import _edit_hitl_message, _hitl_messages
            _hitl_messages.clear()
            _hitl_messages["tokexp"] = (999, 42, "Original approval text")

            await _edit_hitl_message("tokexp", "Expired")

        # Should have called Telegram API exactly once (combined editMessageText + reply_markup)
        assert mock_session.post.call_count == 1
        call_args = mock_session.post.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "editMessageText" in url
        body = call_args[1].get("json", {})
        assert "reply_markup" in body
        assert body["reply_markup"] == {"inline_keyboard": []}
        # Token should be removed from tracking
        assert "tokexp" not in _hitl_messages

    async def test_edit_hitl_message_removes_tracked_message(self) -> None:
        """After editing, the token should be removed from _hitl_messages."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("server.aiohttp.ClientSession", return_value=mock_session), \
             patch("server._get_telegram_token", return_value="bot123"), \
             patch("server.config") as mock_cfg:
            mock_cfg.telegram_owner_chat_id = 999

            from server import _edit_hitl_message, _hitl_messages
            _hitl_messages.clear()
            _hitl_messages["tokrm"] = (999, 55, "Some text")

            await _edit_hitl_message("tokrm", "Approved")

        assert "tokrm" not in _hitl_messages

    async def test_edit_hitl_message_handles_failure_gracefully(self) -> None:
        """On Telegram API error, no crash and token is still removed."""
        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=Exception("API error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("server.aiohttp.ClientSession", return_value=mock_session), \
             patch("server._get_telegram_token", return_value="bot123"), \
             patch("server.config") as mock_cfg:
            mock_cfg.telegram_owner_chat_id = 999

            from server import _edit_hitl_message, _hitl_messages
            _hitl_messages.clear()
            _hitl_messages["tokfail2"] = (999, 66, "Text")

            # Should not raise
            await _edit_hitl_message("tokfail2", "Expired")

        assert "tokfail2" not in _hitl_messages

    async def test_edit_hitl_message_skips_when_token_not_tracked(self) -> None:
        """If the token is not in _hitl_messages, should be a no-op."""
        with patch("server._get_telegram_token", return_value="bot123"):
            from server import _edit_hitl_message, _hitl_messages
            _hitl_messages.clear()

            # Should not raise
            await _edit_hitl_message("nonexistent", "Expired")
