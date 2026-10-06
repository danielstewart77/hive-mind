"""Proactive delivery queue shared by ``mind_server`` and the Telegram bot.

Both run in the same process/event loop (see
``launch_mind_server_and_bots.py``), so unsolicited assistant output produced
by the harness subprocess when no inbound request is draining stdout can be
handed to the bot in-process via this module-level queue.

It is also where an answer the bot could not deliver lands, which is what
makes the scheduling here matter rather than being an implementation detail.

**Items carry a due time, and the earliest due item is served first.** A
failed delivery goes back with a due time minutes out, so the consumer can
work on everything else in the meantime. A plain FIFO with the consumer
sleeping between retries looks like round-robin and is not: the sleep is
global, so one message with nowhere to go — the bot blocked, the chat deleted
— delays *every* other answer by the retry interval, and with N items queued
during an outage the whole queue sheds one attempt per interval rather than
one per item.

Kept free of imports from either side so it can be imported by ``mind_server``
and ``bots.telegram_bot`` without a circular dependency.
"""

import asyncio
import itertools
import time

# How long `get` waits when the only items pending are not due yet. Fresh
# items are picked up within this, so it is a responsiveness floor rather than
# a poll interval — nothing is retried on this cadence.
_IDLE_TICK_S = 1.0

# (due, seq, chat_id, text, attempts). `seq` breaks ties so two items with the
# same due time never compare their payloads, and keeps FIFO order among them.
_queue: "asyncio.PriorityQueue" = asyncio.PriorityQueue()
_seq = itertools.count()


def enqueue(chat_id: int, text: str, attempts: int = 0, delay: float = 0.0) -> None:
    """Enqueue an assistant text turn for delivery to ``chat_id``.

    ``attempts`` is how many times delivery has already been tried and
    ``delay`` how long to hold it before the next one; both are passed only by
    the consumer putting a failed item back.
    """
    _queue.put_nowait(
        (time.monotonic() + delay, next(_seq), chat_id, text, attempts)
    )


async def get() -> "tuple[int, str, int]":
    """Await the next due ``(chat_id, text, attempts)``.

    An item whose due time has not arrived goes straight back, and the wait
    that follows is interruptible by anything sooner — so a message waiting
    out its retry interval never holds up one that just arrived.
    """
    while True:
        due, seq, chat_id, text, attempts = await _queue.get()
        remaining = due - time.monotonic()
        if remaining <= 0:
            return chat_id, text, attempts
        _queue.put_nowait((due, seq, chat_id, text, attempts))
        await asyncio.sleep(min(remaining, _IDLE_TICK_S))


def pending() -> int:
    """How many items are waiting, due or not."""
    return _queue.qsize()


def drain() -> "list[tuple[int, str, int]]":
    """Take everything pending, leaving the queue empty.

    Used at shutdown. The queue is process memory, so a restart with items in
    it loses them — and losing them silently is the exact failure the delivery
    guarantee exists to close, reached through the one action the operator
    performs by hand.
    """
    items = []
    while True:
        try:
            _due, _seq, chat_id, text, attempts = _queue.get_nowait()
        except asyncio.QueueEmpty:
            return items
        items.append((chat_id, text, attempts))


def _reset() -> None:
    """Test helper — drain any pending items so tests start from empty."""
    global _queue
    _queue = asyncio.PriorityQueue()
