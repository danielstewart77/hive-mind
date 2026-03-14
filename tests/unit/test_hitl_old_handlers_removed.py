"""Tests to verify the HITL callback handler exists in telegram_bot."""


class TestHitlCallbackHandler:
    """Verify that the inline-button callback handler is present."""

    def test_handle_hitl_callback_exists(self) -> None:
        """handle_hitl_callback should exist in telegram_bot."""
        from clients import telegram_bot
        assert hasattr(telegram_bot, "handle_hitl_callback")
