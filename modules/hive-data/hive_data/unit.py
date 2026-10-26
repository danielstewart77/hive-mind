"""A unit of work: several operations, all of them or none.

One operation is atomic on its own and needs nothing from here. This is for
the sequences the first consumer already runs and cannot run safely today -
the terminal adopt that deletes a surface binding and then writes a new one,
the rotation that clears three columns and inserts a successor.

Two things it refuses, both because of the shape of the code that will use
it. It will not be opened inside an async generator, and it will not be
handed to a second task.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from . import pool as _pool
from .errors import NoConnectionAvailable, TimedOut, TransactionMisuse
from .rows import as_row, as_rows, row_count, written

log = logging.getLogger("hive-data")

#: Python's own flag for "this code object is an async generator".
_ASYNC_GENERATOR = inspect.CO_ASYNC_GENERATOR


class Unit:
    """One connection, inside one transaction, for the caller's sequence.

    Every operation in this package takes an optional unit. Passed one, it
    runs on that connection inside that transaction; passed none, it runs on
    its own connection and commits by itself.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._task = asyncio.current_task()

    @property
    def connection(self) -> Any:
        """The pinned connection, after checking who is asking.

        A unit handed to a second task is two tasks interleaving statements
        on one connection inside one transaction - so one task's failure
        rolls back the other's committed-looking work, and asyncpg raises
        about an operation already in progress if they overlap at all.
        """
        if asyncio.current_task() is not self._task:
            raise TransactionMisuse(
                "this unit of work was opened by another task. A transaction "
                "is one connection, so sharing it means two tasks writing "
                "inside one another's transaction and each able to roll back "
                "the other's work. Open a unit per task.")
        return self._connection

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return as_rows(await self.connection.fetch(sql, *args))

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return as_row(await self.connection.fetchrow(sql, *args))

    async def execute(self, sql: str, *args: Any) -> dict[str, Any]:
        return written(row_count(await self.connection.execute(sql, *args)))

    async def execute_returning(
        self, sql: str, *args: Any,
    ) -> dict[str, Any]:
        row = as_row(await self.connection.fetchrow(sql, *args))
        return written(0 if row is None else 1, row)

    async def hold(self, key: str) -> None:
        """Serialize this unit against anything else holding the same key.

        Taken inside the transaction (`pg_advisory_xact_lock`), so it is
        released by the commit or the rollback and cannot be leaked by a
        process that dies holding it.

        This is what replaces the first consumer's in-process
        `asyncio.Lock`es. Those are per process, and the only reason they
        sufficed is that comms is the sole writer holding SQLite's
        read-write lock for the life of its container - which is the exact
        property Postgres removes. The adopt sequence run twice concurrently
        leaves two active rows for one session today, and the primary key
        catches nothing.
        """
        await self.connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key)


def _opened_inside_async_generator(depth: int) -> bool:
    frame = inspect.currentframe()
    for _ in range(depth):
        if frame is None:
            return False
        frame = frame.f_back
    return bool(frame and frame.f_code.co_flags & _ASYNC_GENERATOR)


@asynccontextmanager
async def transaction(
    *, timeout: float | None = None,
) -> AsyncIterator[Unit]:
    """Open a unit of work. Everything inside it lands, or none of it does.

    The timeout is a ceiling on the whole unit, not on one statement. A
    transaction held open pins a pooled connection, and enough of those
    pinned at once is a process where every other operation waits on
    `acquire` behind work that is never going to finish.
    """
    if _opened_inside_async_generator(3):
        raise TransactionMisuse(
            "a unit of work cannot be opened in the body of an async "
            "generator. The generator yields to its caller while holding "
            "one pooled connection inside an open transaction, and a caller "
            "that abandons it - a closed browser tab on a streaming reply - "
            "leaves the transaction to unwind whenever the event loop's "
            "asyncgen finalizer reaches it. Do the writes in a unit of their "
            "own, outside the generator, or await a coroutine that opens "
            "one.")
    settings = _pool.settings_in_use()
    limit = timeout if timeout is not None else (
        settings.transaction_timeout if settings else None)

    pool = _pool.live_pool()
    assert settings is not None
    try:
        connection = await asyncio.wait_for(
            pool.acquire(), timeout=settings.acquire_timeout)
    except asyncio.TimeoutError:
        raise NoConnectionAvailable(
            f"every connection in the pool of {settings.pool_max} was busy "
            f"for {settings.acquire_timeout:.1f}s, so this unit of work "
            "never started.") from None
    unit = Unit(connection)
    try:
        async with _deadline(limit):
            transaction_scope = connection.transaction()
            await transaction_scope.start()
            try:
                yield unit
            except BaseException:
                await transaction_scope.rollback()
                raise
            else:
                await transaction_scope.commit()
    except BaseException as exc:
        raise _pool.translate(exc) from exc
    finally:
        await pool.release(connection)


@asynccontextmanager
async def _deadline(seconds: float | None) -> AsyncIterator[None]:
    """A ceiling on the block inside, or no ceiling if none was configured."""
    if seconds is None:
        yield
        return
    try:
        async with asyncio.timeout(seconds):
            yield
    except asyncio.TimeoutError:
        raise TimedOut(
            f"this unit of work ran longer than {seconds:.1f}s and was "
            "rolled back. A transaction holds a pooled connection, so one "
            "that never finishes is a connection no other operation can "
            "have.") from None
