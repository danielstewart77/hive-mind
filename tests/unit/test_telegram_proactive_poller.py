"""Unit tests for the Telegram proactive-delivery poller.

The poller GETs the mind backend's ``/proactive`` endpoint on an interval and
posts each ``{chat_id, text}`` item to Telegram, splitting over the 4096-char
limit. Backend errors and empty responses must never kill the loop.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from bots import telegram_bot


class _FakeResp:
    def __init__(self, status=200, payload=None, raise_exc=None):
        self.status = status
        self._payload = payload if payload is not None else []
        self._raise = raise_exc

    async def json(self):
        return self._payload

    async def __aenter__(self):
        if self._raise is not None:
            raise self._raise
        return self

    async def __aexit__(self, *a):
        return False


class _FakeHttp:
    """Serves a queued sequence of responses to successive .get() calls."""

    def __init__(self, responses):
        self._responses = list(responses)

    def get(self, url, headers=None, timeout=None):
        if self._responses:
            return self._responses.pop(0)
        # After the scripted responses, hand back an empty 200 forever.
        return _FakeResp(status=200, payload=[])


async def _run_poller_briefly(app, monkeypatch, responses, ticks=0.2):
    monkeypatch.setattr(telegram_bot, "http", _FakeHttp(responses))
    monkeypatch.setattr(telegram_bot, "MIND_BACKEND_URL", "http://ada:8420")
    monkeypatch.setattr(telegram_bot, "COMMS_BEARER_TOKEN", "tok")
    monkeypatch.setattr(telegram_bot, "PROACTIVE_POLL_INTERVAL", 0.01)
    task = asyncio.create_task(telegram_bot._proactive_poller(app))
    await asyncio.sleep(ticks)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_poller_posts_item_to_correct_chat(monkeypatch):
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    responses = [_FakeResp(200, [{"chat_id": 4242, "text": "unsolicited turn"}])]

    await _run_poller_briefly(app, monkeypatch, responses)

    app.bot.send_message.assert_any_await(chat_id=4242, text="unsolicited turn")


async def test_poller_splits_long_messages(monkeypatch):
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    long_text = "x" * (telegram_bot.TELEGRAM_MSG_LIMIT + 100)
    responses = [_FakeResp(200, [{"chat_id": 7, "text": long_text}])]

    await _run_poller_briefly(app, monkeypatch, responses)

    # At least the two chunks for the long message were sent.
    calls = app.bot.send_message.await_args_list
    chunks = [c.kwargs["text"] for c in calls if c.kwargs["chat_id"] == 7]
    assert "".join(chunks[:2]) == long_text
    for c in calls:
        assert len(c.kwargs["text"]) <= telegram_bot.TELEGRAM_MSG_LIMIT


async def test_poller_survives_backend_http_error(monkeypatch):
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    responses = [
        _FakeResp(status=500, payload=[]),  # backend error, skipped
        _FakeResp(status=200, payload=[{"chat_id": 3, "text": "recovered"}]),
    ]

    await _run_poller_briefly(app, monkeypatch, responses, ticks=0.3)

    app.bot.send_message.assert_any_await(chat_id=3, text="recovered")


async def test_poller_survives_get_exception(monkeypatch):
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    responses = [
        _FakeResp(raise_exc=RuntimeError("boom")),  # connection blows up
        _FakeResp(status=200, payload=[{"chat_id": 5, "text": "after boom"}]),
    ]

    await _run_poller_briefly(app, monkeypatch, responses, ticks=0.3)

    app.bot.send_message.assert_any_await(chat_id=5, text="after boom")


async def test_poller_disabled_without_backend_url(monkeypatch):
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    monkeypatch.setattr(telegram_bot, "MIND_BACKEND_URL", "")
    # Should return immediately without touching http or the bot.
    await asyncio.wait_for(telegram_bot._proactive_poller(app), timeout=1.0)
    app.bot.send_message.assert_not_awaited()


async def test_poller_skips_items_missing_fields(monkeypatch):
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    responses = [_FakeResp(200, [{"chat_id": None, "text": "no chat"}, {"chat_id": 8}])]

    await _run_poller_briefly(app, monkeypatch, responses)

    # Neither malformed item should have been sent.
    for c in app.bot.send_message.await_args_list:
        assert c.kwargs["chat_id"] not in (None, 8) or c.kwargs.get("text")
    # Concretely: no send with chat_id 8 (missing text) and none with None.
    sent_chats = [c.kwargs["chat_id"] for c in app.bot.send_message.await_args_list]
    assert 8 not in sent_chats
    assert None not in sent_chats
