"""Helpers for tests that make a gated call and leave it held.

A gated call stays open while it waits for a person, so a test that asks and
then decides has to do both at once. The app is the real one, called through
its own ASGI interface on the suite's event loop; nothing of the route, the
gate or the store is replaced.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

import store

LABEL = {"name": "Receipts/2026"}


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def an_app() -> tuple[Any, httpx.AsyncClient]:
    """The real app with its store open (the test's `db_conn` opened it)."""
    import hitl
    from server import create_app

    app = create_app()
    # What the app's start-up does, which a bare ASGI transport does not run.
    await hitl.seed_default_hitl_settings()
    await hitl.sync_tool_registry()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="http://tools.test")
    return app, client


async def the_waiting_request(wait: float = 5.0) -> str:
    """The id of the one request now waiting on a person."""
    import time

    deadline = asyncio.get_running_loop().time() + wait
    while asyncio.get_running_loop().time() < deadline:
        rows = await store.waiting_requests(int(time.time()))
        if rows:
            return str(rows[0]["id"])
        await asyncio.sleep(0.01)
    raise AssertionError("no request came to be waiting on a person")


class Clock:
    """A clock the hold reads, moved by the hold's own pauses.

    Replaces the two places the hold touches real time (`hitl._now` and
    `hitl._pause`): the wait is spent by advancing this, so a five minute
    timeout is run through in an instant and no test sleeps through one.
    """

    def __init__(self, start: float) -> None:
        self.t = int(start)
        self.pauses: list[float] = []

    def now(self) -> int:
        return self.t

    async def pause(self, event: asyncio.Event, seconds: float) -> None:
        self.pauses.append(seconds)
        self.t += max(1, int(seconds))
        await asyncio.sleep(0)
