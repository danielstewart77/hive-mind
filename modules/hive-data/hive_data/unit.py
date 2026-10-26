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
        #: Whether any statement has run on this unit yet. `hold` is only
        #: meaningful as the first one.
        self._used = False

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

    async def _run(self, method: str, sql: str, *args: Any) -> Any:
        """One statement, with this package's error taxonomy applied.

        Applied per statement rather than only at the transaction boundary.
        Translating at the boundary alone means the exception a caller sees
        from an operation it passed `unit=` is a raw asyncpg class, so
        `except Conflict` does not fire and the handler above it turns a
        duplicate into a 500 — which is the SMS-gateway retry loop the whole
        taxonomy was written to prevent, reappearing inside the one
        construct that is supposed to be safest.
        """
        connection = self.connection
        self._used = True
        try:
            return await getattr(connection, method)(sql, *args)
        except BaseException as exc:
            raise _pool.translate(exc) from exc

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return as_rows(await self._run("fetch", sql, *args))

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return as_row(await self._run("fetchrow", sql, *args))

    async def execute(self, sql: str, *args: Any) -> dict[str, Any]:
        return written(row_count(await self._run("execute", sql, *args)))

    async def execute_returning(
        self, sql: str, *args: Any,
    ) -> dict[str, Any]:
        row = as_row(await self._run("fetchrow", sql, *args))
        return written(0 if row is None else 1, row)

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """Scope a failure that the caller intends to recover from.

        Postgres aborts the whole transaction on the first error, so the
        gateway's prevailing shape — try the write, log the failure, carry
        on, which is legal against SQLite — makes every later statement in
        the unit refuse without being read. A savepoint is what lets a
        handled `Conflict` stay handled.
        """
        scope = self.connection.transaction()
        await scope.start()
        try:
            yield
        except BaseException:
            await scope.rollback()
            raise
        else:
            await scope.commit()

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
        if self._used:
            raise TransactionMisuse(
                "take the hold before anything else in this unit. A hold "
                "placed after a read does not protect that read: two units "
                "both read an unclaimed row, then both take the lock in "
                "turn, and both write — which is two owners for one bot, "
                "the exact outcome this method exists to prevent.")
        self._used = True
        try:
            await self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key)
        except BaseException as exc:
            raise _pool.translate(exc) from exc

    async def hold_conversation(self, conversation_id: str) -> None:
        """Serialize this unit against anything else working on one
        conversation.

        A named key rather than a string the caller invents, because two
        call sites spelling it differently do not serialize and nothing
        anywhere says so.
        """
        await self.hold(f"hive:conversation:{conversation_id}")


#: How far out to look for an async generator. Deep enough to cross the
#: helper an author adds when the guard fires, and the `AsyncExitStack` that
#: enters the context for them; bounded so the check stays cheap.
_STACK_CEILING = 40


def _opened_inside_async_generator(skip: int) -> str | None:
    """The name of the async generator this is being opened inside, if any.

    The whole stack rather than one frame. A fixed depth is right for
    `async with transaction()` written literally and for nothing else — not
    for a write moved into a helper, which is the natural reaction to the
    guard firing, and not for `AsyncExitStack.enter_async_context`, which
    enters the context on the caller's behalf one frame away.
    """
    frame = inspect.currentframe()
    for _ in range(skip):
        if frame is None:
            return None
        frame = frame.f_back
    seen = 0
    while frame is not None and seen < _STACK_CEILING:
        if frame.f_code.co_flags & _ASYNC_GENERATOR:
            return frame.f_code.co_qualname
        frame = frame.f_back
        seen += 1
    return None


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
    generator = _opened_inside_async_generator(3)
    if generator is not None:
        raise TransactionMisuse(
            f"a unit of work cannot be opened inside the async generator "
            f"{generator}. The generator yields to its caller while holding "
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


#: Advisory-lock keys are namespaced so two call sites cannot spell one
#: conversation differently and silently fail to serialize.
def conversation_key(conversation_id: str) -> str:
    return f"hive:conversation:{conversation_id}"


@asynccontextmanager
async def conversation_lock(
    conversation_id: str, *, wait: bool = True,
) -> AsyncIterator[bool]:
    """Serialize work on one conversation across processes, without a unit.

    A unit of work cannot be opened inside an async generator, and the
    gateway's `send_message` is an async generator that holds its
    conversation lock across the yields — so the lock that most needs
    replacing is the one `Unit.hold` cannot reach. This is the same advisory
    lock at session scope instead of transaction scope: it pins a pooled
    connection for as long as it is held, which is inherent to a
    cross-process lock, but it holds no open transaction, no snapshot and
    nothing to roll back.

    `wait=False` is the shape the gateway's turn lock actually has: a
    non-blocking test that answers "this conversation is mid-answer" rather
    than queueing. Yields False when the lock was not taken, so the caller
    can say so. Through a blocking lock that check would become a silent
    wait ending in a timeout, which tells the user nothing.
    """
    pool = _pool.live_pool()
    settings = _pool.settings_in_use()
    assert settings is not None
    key = conversation_key(conversation_id)
    try:
        connection = await asyncio.wait_for(
            pool.acquire(), timeout=settings.acquire_timeout)
    except asyncio.TimeoutError:
        raise NoConnectionAvailable(
            f"every connection in the pool of {settings.pool_max} was busy "
            f"for {settings.acquire_timeout:.1f}s, so the lock on "
            f"{conversation_id} was never attempted.") from None
    taken = False
    try:
        if wait:
            await connection.execute(
                "SELECT pg_advisory_lock(hashtextextended($1, 0))", key)
            taken = True
        else:
            taken = bool(await connection.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key))
        yield taken
    except BaseException as exc:
        raise _pool.translate(exc) from exc
    finally:
        # Released explicitly rather than left to the connection going back
        # to the pool: a session-scope advisory lock outlives the statement,
        # so a connection returned still holding one hands the next borrower
        # a lock nobody asked for and nobody will release.
        if taken:
            try:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)
            except Exception:  # pragma: no cover - the connection is going
                log.warning(
                    "hive-data: could not release the advisory lock on %s; "
                    "terminating the connection so the lock cannot outlive "
                    "it", conversation_id)
                connection.terminate()
        await pool.release(connection)
