"""Unit tests for the Telegram proactive-delivery consumer.

The consumer drains hive_surfaces.proactive and posts each item via the Application's
bot, splitting messages over Telegram's 4096-char limit.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive_surfaces import proactive
from hive_surfaces import telegram_bot


@pytest.fixture(autouse=True)
def _fresh_queue():
    proactive._reset()
    yield
    proactive._reset()


async def _run_consumer_until_drained(app):
    task = asyncio.create_task(telegram_bot._proactive_consumer(app))
    # Give the consumer a few loop turns to drain the queue.
    for _ in range(50):
        await asyncio.sleep(0.005)
        if proactive._queue.empty():
            break
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_consumer_posts_item_to_correct_chat():
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    proactive.enqueue(4242, "unsolicited turn")

    await _run_consumer_until_drained(app)

    app.bot.send_message.assert_awaited_once_with(chat_id=4242, text="unsolicited turn")


async def test_consumer_splits_long_messages():
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    long_text = "x" * (telegram_bot.TELEGRAM_MSG_LIMIT + 100)
    proactive.enqueue(7, long_text)

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_count == 2
    sent = "".join(c.kwargs["text"] for c in app.bot.send_message.await_args_list)
    assert sent == long_text
    for call in app.bot.send_message.await_args_list:
        assert call.kwargs["chat_id"] == 7
        assert len(call.kwargs["text"]) <= telegram_bot.TELEGRAM_MSG_LIMIT


async def test_a_failed_send_is_retried_rather_than_dropped(monkeypatch):
    """Requirement 13: an answer arrives late rather than never.

    This queue is where an undeliverable button answer lands, so a single
    failed attempt followed by a shrug is the bug, not the behaviour: the
    operator acted, the action happened, and the sentence saying so is gone.
    The first send raises and the second succeeds — one message, two attempts.
    """
    monkeypatch.setattr(telegram_bot, "_PROACTIVE_RETRY_S", 0)
    app = MagicMock()
    app.bot.send_message = AsyncMock(side_effect=[RuntimeError("boom"), None])
    proactive.enqueue(1, "will land on the retry")

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_count == 2
    assert app.bot.send_message.await_args.kwargs["text"] == "will land on the retry"


async def test_retrying_spans_an_outage_measured_in_hours(monkeypatch):
    """Requirement 13: the budget has to match the failure it was written for.

    The 2026-09-17 outage ran for hours. A retry budget measured in seconds
    does not survive it — it only moves where the answer is lost — so the
    attempts and the interval together have to cover an outage of that shape.
    """
    assert telegram_bot._PROACTIVE_MAX_ATTEMPTS * telegram_bot._PROACTIVE_RETRY_S >= 3600


async def test_a_failing_message_goes_to_the_back_of_the_queue(monkeypatch):
    """Requirement 13: a stuck message must not hold up the ones behind it.

    Retrying in place is simpler and wrong: with a budget spanning hours, one
    undeliverable item would silence every answer queued behind it for hours
    too. The second message goes out while the first is still being retried.
    """
    monkeypatch.setattr(telegram_bot, "_PROACTIVE_RETRY_S", 0)
    app = MagicMock()
    app.bot.send_message = AsyncMock(
        side_effect=[RuntimeError("down"), None, None]
    )
    proactive.enqueue(1, "keeps failing")
    proactive.enqueue(2, "behind it")

    await _run_consumer_until_drained(app)

    delivered = [c.kwargs["text"] for c in app.bot.send_message.await_args_list]
    # The second message was attempted before the first was retried.
    assert delivered.index("behind it") < len(delivered) - 1 or delivered[-1] == "keeps failing"
    assert "behind it" in delivered


async def test_a_permanently_undeliverable_message_is_given_up_on(monkeypatch):
    """Requirement 13: retrying is bounded, because the queue has to drain.

    A message with nowhere to go — the bot blocked, the chat deleted — retried
    forever is a consumer that never empties. It is abandoned to the journal,
    which is a worse place than the operator's phone and a far better one than
    nowhere.
    """
    monkeypatch.setattr(telegram_bot, "_PROACTIVE_RETRY_S", 0)
    monkeypatch.setattr(telegram_bot, "_PROACTIVE_MAX_ATTEMPTS", 3)
    app = MagicMock()
    app.bot.send_message = AsyncMock(side_effect=RuntimeError("blocked"))
    proactive.enqueue(1, "never deliverable")

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_count == 3
    assert proactive._queue.empty()


async def test_what_is_still_queued_at_shutdown_reaches_the_journal(caplog):
    """Requirement 13: a restart must not lose an answer without a trace.

    The queue is process memory, and restarting the service is the one action
    the operator performs by hand. Dropping pending answers silently there is
    the same loss this whole path exists to close.
    """
    import logging

    proactive.enqueue(4242, "the answer you never saw")

    with caplog.at_level(logging.WARNING):
        await telegram_bot._on_shutdown(MagicMock())

    assert any("the answer you never saw" in r.getMessage() for r in caplog.records)
    assert proactive._queue.empty()


async def test_chat_not_found_is_abandoned_without_retrying(monkeypatch):
    """A destination that does not exist is not an outage.

    "Chat not found" is Telegram's answer about the chat id itself, so no
    amount of waiting changes it. Given the outage budget it cost 60 attempts
    over three hours, each logging a warning — 180 lines of one NetSage window
    on 2026-10-15, for one message that was never deliverable.
    """
    from telegram.error import BadRequest

    monkeypatch.setattr(telegram_bot, "_PROACTIVE_RETRY_S", 0)
    app = MagicMock()
    app.bot.send_message = AsyncMock(side_effect=BadRequest("Chat not found"))
    proactive.enqueue(1556059743968039065, "nowhere to go")

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_count == 1, (
        "a permanently invalid destination is attempted once, not sixty times"
    )
    assert proactive._queue.empty()


async def test_a_blocked_bot_is_abandoned_without_retrying(monkeypatch):
    """Being blocked is a decision, not an outage. Retrying it for three hours
    tells the operator nothing and fills the journal with a line the sensor
    reads as an anomaly."""
    from telegram.error import Forbidden

    monkeypatch.setattr(telegram_bot, "_PROACTIVE_RETRY_S", 0)
    app = MagicMock()
    app.bot.send_message = AsyncMock(
        side_effect=Forbidden("Forbidden: bot was blocked by the user")
    )
    proactive.enqueue(42, "blocked")

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_count == 1
    assert proactive._queue.empty()


async def test_an_outage_still_gets_the_full_retry_budget(monkeypatch):
    """The permanent-failure check must not cost a real outage its retries.

    A NetworkError is exactly what the hours-long budget was written for, so
    it keeps retrying where "Chat not found" stops.
    """
    from telegram.error import NetworkError

    monkeypatch.setattr(telegram_bot, "_PROACTIVE_RETRY_S", 0)
    app = MagicMock()
    app.bot.send_message = AsyncMock(
        side_effect=[NetworkError("connection reset"), None]
    )
    proactive.enqueue(8776938611, "arrives late")

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_count == 2
    assert app.bot.send_message.await_args.kwargs["text"] == "arrives late"


async def test_a_bad_request_about_the_message_still_retries(monkeypatch):
    """Only a destination verdict is permanent. A BadRequest naming something
    else is not read as one, because the class is shared and guessing wide
    would abandon answers an outage would have delivered."""
    from telegram.error import BadRequest

    monkeypatch.setattr(telegram_bot, "_PROACTIVE_RETRY_S", 0)
    app = MagicMock()
    app.bot.send_message = AsyncMock(
        side_effect=[BadRequest("Message_thread_not_found"), None]
    )
    proactive.enqueue(7, "retried")

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_count == 2
