"""Unit tests for handle_hitl_callback in telegram_bot.py.

Tests the CallbackQueryHandler that processes inline keyboard button taps
for HITL approve/deny actions.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_callback_query(
    data: str,
    user_id: int = 123,
    message_text: str = "\U0001f514 Approval Required\n\nSome action summary",
):
    """Create a mock CallbackQuery for testing."""
    query = MagicMock()
    query.data = data
    query.from_user.id = user_id
    query.message.text = message_text
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return query


def _make_update_with_callback(query):
    """Create a mock Update with a callback_query."""
    update = MagicMock()
    update.callback_query = query
    return update


def _make_context():
    """Create a mock context object."""
    context = MagicMock()
    return context


@pytest.mark.asyncio
class TestHandleHitlCallback:
    """Tests for the handle_hitl_callback function."""

    async def test_handle_hitl_callback_approve_posts_to_gateway(self) -> None:
        """Approving should POST to /hitl/respond with approved=true."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_approve_abc123")
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "secret123"
            mock_cfg.server_port = 8420

            await handle_hitl_callback(update, context)

        # POST goes to /hitl/{token}/respond with body {"action": "approve"|"deny"}
        call_args = mock_http.post.call_args
        url = call_args[0][0]
        body = call_args[1].get("json") or call_args[0][1]
        assert "/hitl/abc123/respond" in url
        assert body["action"] == "approve"

    async def test_handle_hitl_callback_deny_posts_to_gateway(self) -> None:
        """Denying should POST to /hitl/respond with approved=false."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_deny_abc123")
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "secret123"
            mock_cfg.server_port = 8420

            await handle_hitl_callback(update, context)

        call_args = mock_http.post.call_args
        url = call_args[0][0]
        body = call_args[1].get("json") or call_args[0][1]
        assert "/hitl/abc123/respond" in url
        assert body["action"] == "deny"

    async def test_handle_hitl_callback_edits_message_on_approve(self) -> None:
        """After approval, the message should be edited to show Approved."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_approve_tok1")
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "secret123"
            mock_cfg.server_port = 8420

            await handle_hitl_callback(update, context)

        edit_text = query.edit_message_text.call_args[0][0]
        assert "Approved" in edit_text

    async def test_handle_hitl_callback_edits_message_on_deny(self) -> None:
        """After denial, the message should be edited to show Denied."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_deny_tok2")
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "secret123"
            mock_cfg.server_port = 8420

            await handle_hitl_callback(update, context)

        edit_text = query.edit_message_text.call_args[0][0]
        assert "Denied" in edit_text

    async def test_handle_hitl_callback_removes_keyboard(self) -> None:
        """After action, the inline keyboard should be removed."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_approve_tok3")
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "secret123"
            mock_cfg.server_port = 8420

            await handle_hitl_callback(update, context)

        # The handler now removes the keyboard by calling edit_message_text
        # with reply_markup=None on its first call (the "Processing…" frame).
        first_call_kwargs = query.edit_message_text.call_args_list[0].kwargs
        assert first_call_kwargs.get("reply_markup") is None

    async def test_handle_hitl_callback_answers_query(self) -> None:
        """query.answer() must be called to dismiss the loading spinner."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_approve_tok4")
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "secret123"
            mock_cfg.server_port = 8420

            await handle_hitl_callback(update, context)

        query.answer.assert_called()

    async def test_handle_hitl_callback_handles_expired_token(self) -> None:
        """When gateway returns 404 (expired), should answer query and edit message."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_approve_expired1")
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.json = AsyncMock(return_value={"error": "invalid or expired token"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "secret123"
            mock_cfg.server_port = 8420

            await handle_hitl_callback(update, context)

        # Should answer the query
        query.answer.assert_called()
        # Final edit shows the gateway's error status
        edit_text = query.edit_message_text.call_args[0][0]
        assert "404" in edit_text

    async def test_handle_hitl_callback_rejects_unauthorized_user(self) -> None:
        """Unauthorized user should get 'Not authorized' and no POST made."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_approve_tok5", user_id=999)
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_http = MagicMock()
        mock_http.post = MagicMock()

        with patch("clients.telegram_bot._is_allowed_user", return_value=False), \
             patch("clients.telegram_bot.http", mock_http):

            await handle_hitl_callback(update, context)

        query.answer.assert_called_once()
        answer_text = query.answer.call_args[0][0] if query.answer.call_args[0] else query.answer.call_args[1].get("text", "")
        assert "Not authorized" in answer_text
        mock_http.post.assert_not_called()

    async def test_handle_hitl_callback_double_tap_silently_ignored(self) -> None:
        """Double-tap (404 from gateway) should be handled silently."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_approve_resolved1")
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.json = AsyncMock(return_value={"error": "invalid or expired token"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "secret123"
            mock_cfg.server_port = 8420

            # Should not raise
            await handle_hitl_callback(update, context)

        # query.answer should be called (dismiss spinner)
        query.answer.assert_called()

    async def test_handle_hitl_callback_removes_keyboard_on_non_200(self) -> None:
        """When gateway returns non-200 (expired/double-tap), keyboard should be removed."""
        from clients.telegram_bot import handle_hitl_callback

        query = _make_callback_query("hitl_approve_expired2")
        update = _make_update_with_callback(query)
        context = _make_context()

        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.json = AsyncMock(return_value={"error": "invalid or expired token"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "secret123"
            mock_cfg.server_port = 8420

            await handle_hitl_callback(update, context)

        # Keyboard removed via edit_message_text(reply_markup=None) on the
        # "Processing…" frame, before the gateway responds with 404.
        first_call_kwargs = query.edit_message_text.call_args_list[0].kwargs
        assert first_call_kwargs.get("reply_markup") is None
