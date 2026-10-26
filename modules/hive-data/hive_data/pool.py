"""The connection pool, and the only place a driver exception is read.

Opening is explicit and idempotent. Nothing here opens a pool on first use:
a lazy open under two concurrent first calls builds two pools, holds double
the connections against a ceiling shared with every other service, and
reports nothing - so the first operation against an unopened package is an
error that says so.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Awaitable, Callable, TypeVar

import asyncpg

from .config import Settings, from_environment
from .errors import (
    CredentialRefused,
    Conflict,
    DatabaseMissing,
    HiveDataError,
    MissingReference,
    NoConnectionAvailable,
    NotInitialised,
    TimedOut,
    Unreachable,
    ValueRefused,
    WriteRejected,
)

log = logging.getLogger("hive-data")

T = TypeVar("T")

#: The live pool and the settings it was opened with. Module-level because a
#: process has one database, and keyed by its settings so that reconfiguring
#: and re-opening cannot hand back a pool pointed somewhere else.
_pool: asyncpg.Pool | None = None
_settings: Settings | None = None
#: Guards open and close against each other and against themselves.
_gate = asyncio.Lock()


def translate(exc: BaseException) -> HiveDataError:
    """A driver exception as the thing the caller has to act on.

    Only a driver exception. Anything this function does not recognise is
    re-raised unchanged rather than wrapped, because a bug in our own code
    must not arrive as an outcome the caller is ready to handle.

    One place, rather than a `try` per operation: a taxonomy applied in a
    hundred places is a taxonomy with a hundred chances to fold two outcomes
    into one.
    """
    if isinstance(exc, HiveDataError):
        return exc
    if isinstance(exc, asyncpg.InvalidPasswordError):
        return CredentialRefused(str(exc))
    if isinstance(exc, asyncpg.InvalidAuthorizationSpecificationError):
        return CredentialRefused(str(exc))
    if isinstance(exc, asyncpg.InvalidCatalogNameError):
        return DatabaseMissing(str(exc))
    if isinstance(exc, asyncpg.UniqueViolationError):
        return Conflict(str(exc))
    if isinstance(exc, asyncpg.ForeignKeyViolationError):
        return MissingReference(str(exc))
    if isinstance(exc, (asyncpg.CheckViolationError,
                        asyncpg.NotNullViolationError,
                        asyncpg.DataError)):
        return ValueRefused(str(exc))
    if isinstance(exc, asyncpg.QueryCanceledError):
        return TimedOut(str(exc))
    if isinstance(exc, asyncpg.IntegrityConstraintViolationError):
        return WriteRejected(str(exc))
    if isinstance(exc, asyncio.TimeoutError):
        return TimedOut("the database did not answer inside the deadline")
    if isinstance(exc, (OSError, socket.gaierror,
                        asyncpg.CannotConnectNowError)):
        # A refused connection, a name that does not resolve, a host that is
        # not there. Deliberately not folded in with a refused credential:
        # those are two different machines to go and look at.
        return Unreachable(str(exc) or exc.__class__.__name__)
    if isinstance(exc, asyncpg.PostgresError):
        return WriteRejected(str(exc))
    if isinstance(exc, asyncpg.InterfaceError):
        return Unreachable(str(exc))
    # Anything else is not this package's to rename. A NameError or a
    # TypeError dressed as a HiveDataError is a bug wearing the costume of
    # an outcome the caller has a branch for - and the branch will swallow
    # it. Programming errors propagate as themselves.
    raise exc


async def open_pool(settings: Settings | None = None) -> asyncpg.Pool:
    """Open the pool, or hand back the one already open. Idempotent.

    Idempotent under concurrency, not merely on paper: the gate is taken
    before the check, so two coroutines racing to make the first call end up
    with one pool between them.
    """
    global _pool, _settings
    resolved = settings or from_environment()
    async with _gate:
        if _pool is not None:
            if _settings is not None and _settings.dsn != resolved.dsn:
                raise HiveDataError(
                    "a pool is already open against "
                    f"{_settings.describes}; close it before opening one "
                    f"against {resolved.describes}. Two pools in one process "
                    "would let two operations in one request answer from two "
                    "databases.")
            return _pool
        try:
            _pool = await asyncpg.create_pool(
                dsn=resolved.dsn,
                min_size=resolved.pool_min,
                max_size=resolved.pool_max,
                timeout=resolved.connect_timeout,
                command_timeout=resolved.statement_timeout,
                # Set on the server rather than only in the client, because
                # the client holding a statement open may be the process that
                # has stopped running. Milliseconds, as Postgres counts them.
                server_settings={
                    "statement_timeout":
                        str(int(resolved.statement_timeout * 1000)),
                    "idle_in_transaction_session_timeout":
                        str(int(resolved.transaction_timeout * 1000)),
                    "application_name": "hive-data",
                },
            )
        except BaseException as exc:
            _pool = None
            _settings = None
            raise translate(exc) from exc
        _settings = resolved
    log.info(
        "hive-data: pool open against %s, %d-%d connections, statement "
        "deadline %.1fs", resolved.describes, resolved.pool_min,
        resolved.pool_max, resolved.statement_timeout)
    return _pool


async def close_pool(drain_timeout: float | None = None) -> None:
    """Close the pool, waiting for work in flight up to a deadline.

    A deadline rather than asyncpg's unbounded `close()`: a shutdown that
    hangs on one wedged connection never reaches the rest of the shutdown,
    and the process is killed with its other work half-done.
    """
    global _pool, _settings
    async with _gate:
        pool, settings = _pool, _settings
        _pool = None
        _settings = None
    if pool is None:
        return
    deadline = (settings.transaction_timeout if drain_timeout is None
                and settings is not None else drain_timeout)
    try:
        await asyncio.wait_for(pool.close(), timeout=deadline)
    except asyncio.TimeoutError:
        log.warning(
            "hive-data: pool did not drain in %.1fs; terminating it", deadline)
        pool.terminate()


def settings_in_use() -> Settings | None:
    """The settings the open pool was opened with, if one is open."""
    return _settings


def live_pool() -> asyncpg.Pool:
    """The open pool, or an error naming what was not done."""
    if _pool is None:
        raise NotInitialised(
            "no pool is open. Call `await hive_data.connect()` once at "
            "startup. Nothing opens one on demand, because a lazy open under "
            "two concurrent first calls builds two pools and holds double "
            "the connections with nothing saying so.")
    return _pool


async def run(action: Callable[[Any], Awaitable[T]]) -> T:
    """Hand one pooled connection to `action`, with every deadline applied.

    Acquisition has its own deadline and its own error. Waiting on `acquire`
    with no deadline is indistinguishable, from outside, from a database that
    has stopped answering - and the remedy is the pool's size rather than the
    server, so the two must not arrive as one exception.
    """
    pool = live_pool()
    assert _settings is not None
    try:
        connection = await asyncio.wait_for(
            pool.acquire(), timeout=_settings.acquire_timeout)
    except asyncio.TimeoutError:
        raise NoConnectionAvailable(
            f"every connection in the pool of {_settings.pool_max} was busy "
            f"for {_settings.acquire_timeout:.1f}s. The database is "
            "answering; this process has run out of connections to ask it "
            "with.") from None
    except BaseException as exc:
        raise translate(exc) from exc
    try:
        return await action(connection)
    except BaseException as exc:
        raise translate(exc) from exc
    finally:
        await pool.release(connection)
