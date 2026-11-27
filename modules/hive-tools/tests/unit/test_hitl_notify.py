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
def test_notify_daniel_carries_the_link_and_no_button(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The notice says where to approve (the hive site page for the request)
    and carries no Approve or Deny button."""
    monkeypatch.setenv("HIVE_SITE_PUBLIC_URL", "https://hive.example/")
    with patch("httpx.post") as mock_post:
        notify_daniel("docker_ops", "bilby", "{}", "req-abc-123")

        payload = mock_post.call_args[1]["json"]
        assert "https://hive.example/approvals/req-abc-123" in payload["text"]
        assert "reply_markup" not in payload


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
