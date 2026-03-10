"""Tests to verify old HITL command handlers have been removed and new callback handler exists."""


class TestOldHandlersRemoved:
    """Verify that the old /approve_ /deny_ handlers are gone."""

    def test_cmd_hitl_approve_not_in_module(self) -> None:
        """cmd_hitl_approve should no longer exist in telegram_bot."""
        from clients import telegram_bot
        assert not hasattr(telegram_bot, "cmd_hitl_approve")

    def test_cmd_hitl_deny_not_in_module(self) -> None:
        """cmd_hitl_deny should no longer exist in telegram_bot."""
        from clients import telegram_bot
        assert not hasattr(telegram_bot, "cmd_hitl_deny")

    def test_handle_hitl_response_not_in_module(self) -> None:
        """_handle_hitl_response should no longer exist in telegram_bot."""
        from clients import telegram_bot
        assert not hasattr(telegram_bot, "_handle_hitl_response")

    def test_handle_hitl_callback_exists(self) -> None:
        """handle_hitl_callback should exist in telegram_bot."""
        from clients import telegram_bot
        assert hasattr(telegram_bot, "handle_hitl_callback")
