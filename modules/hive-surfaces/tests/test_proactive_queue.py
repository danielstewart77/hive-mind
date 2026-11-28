"""Unit tests for the proactive delivery queue (bots/proactive.py)."""

import asyncio

import pytest

from hive_surfaces import proactive


@pytest.fixture(autouse=True)
def _fresh_queue():
    proactive._reset()
    yield
    proactive._reset()


async def test_enqueue_then_get_round_trips_item():
    proactive.enqueue(12345, "hello there")
    chat_id, text, _attempts = await proactive.get()
    assert chat_id == 12345
    assert text == "hello there"


async def test_fifo_ordering():
    proactive.enqueue(1, "first")
    proactive.enqueue(2, "second")
    assert await proactive.get() == (1, "first", 0)
    assert await proactive.get() == (2, "second", 0)


async def test_get_blocks_until_item_available():
    async def producer():
        await asyncio.sleep(0.01)
        proactive.enqueue(9, "late")

    task = asyncio.create_task(producer())
    chat_id, text, _attempts = await asyncio.wait_for(proactive.get(), timeout=1.0)
    await task
    assert (chat_id, text) == (9, "late")
