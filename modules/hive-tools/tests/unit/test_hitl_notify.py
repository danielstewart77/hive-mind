"""Unit tests for notify_daniel() in hitl.py."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hitl import notify_daniel


@pytest.fixture()
def _telegram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the required Telegram environment variables."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "12345")


@pytest.mark.usefixtures("_telegram_env")
def test_notify_daniel_sends_telegram_message() -> None:
    """notify_daniel() POSTs to the Telegram sendMessage endpoint with the correct payload."""
    with patch("httpx.post") as mock_post:
        notify_daniel("calendar_list_events", "ada", '{"query": ""}', "req-001")

        mock_post.assert_called_once()
        call_args = mock_post.call_args

        # Verify URL
        assert call_args[0][0] == "https://api.telegram.org/botfake-bot-token/sendMessage"

        # Verify payload structure. The implementation produces a friendly,
        # human-readable label (calendar_list_events → "📅 List Events") and
        # records the caller mind. The request_id lives in the callback_data,
        # not in the message body.
        payload = call_args[1]["json"]
        assert payload["chat_id"] == "12345"
        assert "List Events" in payload["text"]
        assert "ada" in payload["text"]


@pytest.mark.usefixtures("_telegram_env")
def test_notify_daniel_includes_inline_keyboard() -> None:
    """The Telegram payload includes reply_markup with an inline_keyboard.

    The current layout is two rows of one button each (Approve, Deny stacked).
    """
    with patch("httpx.post") as mock_post:
        notify_daniel("gmail_send", "ada", "{}", "req-002")

        payload = mock_post.call_args[1]["json"]
        assert "reply_markup" in payload
        assert "inline_keyboard" in payload["reply_markup"]
        keyboard = payload["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 2  # two rows: approve, deny
        assert len(keyboard[0]) == 1  # one button per row
        assert "Approve" in keyboard[0][0]["text"]
        assert "Deny" in keyboard[1][0]["text"]


@pytest.mark.usefixtures("_telegram_env")
def test_notify_daniel_button_callback_data_format() -> None:
    """callback_data follows the pattern hitl_approve_{id} / hitl_deny_{id}."""
    with patch("httpx.post") as mock_post:
        notify_daniel("docker_ops", "bilby", "{}", "req-abc-123")

        payload = mock_post.call_args[1]["json"]
        keyboard = payload["reply_markup"]["inline_keyboard"]
        assert keyboard[0][0]["callback_data"] == "hitl_approve_req-abc-123"
        assert keyboard[1][0]["callback_data"] == "hitl_deny_req-abc-123"


def test_notify_daniel_missing_credentials_fails_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No crash when TELEGRAM_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID is missing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)

    with patch("httpx.post") as mock_post:
        # Should not raise
        notify_daniel("calendar_list_events", "ada", "{}", "req-004")

        # httpx.post should NOT have been called
        mock_post.assert_not_called()
