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
    SchemaMismatch,
    Conflict,
    TransactionAborted,
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

#: The fallback drain deadline, for the path that has no settings to read.
DEFAULT_DRAIN_TIMEOUT = 30.0


def _vector_literal(value: Any) -> str:
    """A sequence of numbers as pgvector's own input syntax."""
    if isinstance(value, str):
        # Already a literal. Passed straight through rather than re-encoded,
        # so a caller holding one from somewhere else is not refused.
        return value
    return "[" + ",".join(repr(float(v)) for v in value) + "]"


def _vector_values(value: str) -> list[float]:
    """pgvector's output syntax as the numbers it stands for."""
    return [float(part) for part in value.strip()[1:-1].split(",") if part]


async def _register_codecs(connection: Any) -> None:
    """Teach a connection the types this package reads that asyncpg lacks.

    Run on every connection the pool opens, which is the only place it can
    be: a pool that grew past its minimum later would otherwise hold some
    connections that know `vector` and some that do not, and the same
    operation would return numbers or a 50,000-character string depending
    on which one it was handed.

    A database with no `vector` type is not an error here. `core` alone is
    a perfectly good install - comms needs none of this - and refusing to
    open a pool for it would make a memory-side type decision break every
    service in the hive.
    """
    # The schema is asked for rather than assumed. `set_type_codec` resolves
    # a type by (schema, name), and `CREATE EXTENSION vector` lands wherever
    # `search_path` pointed when somebody ran it. Hardcoding `public` meant a
    # database with the extension anywhere else registered no codec at all
    # and said nothing — and the startup type check passes there, because
    # `udt_name` is just `vector` in any schema.
    where = await connection.fetchval(
        "SELECT n.nspname FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE t.typname = 'vector'")
    if where is None:
        return
    try:
        await connection.set_type_codec(
            "vector", schema=where,
            encoder=_vector_literal, decoder=_vector_values,
            format="text")
    except (asyncpg.exceptions.UndefinedObjectError, ValueError):
        # asyncpg raises a plain `ValueError: unknown type: public.vector`
        # when the type is not there, not the Postgres error class the name
        # suggests. Both are caught, because a `core`-only database — which
        # is every install that runs comms and not lucent — reaches this.
        return

T = TypeVar("T")

#: The live pool and the settings it was opened with. Module-level because a
#: process has one database, and keyed by its settings so that reconfiguring
#: and re-opening cannot hand back a pool pointed somewhere else.
_pool: asyncpg.Pool | None = None
_settings: Settings | None = None
#: The loop the live pool belongs to. asyncpg ties a pool to the loop that
#: created it, and a pool handed to a second loop fails on its first
#: statement with "Event loop is closed" - which arrives nowhere near the
#: call that reused it.
_loop: asyncio.AbstractEventLoop | None = None

#: Guards open and close against each other and against themselves, per
#: loop. A module-level `asyncio.Lock` only touches its loop on the
#: contended path, so a single shared one works until two coroutines
#: genuinely race on a second loop - and then raises "is bound to a
#: different event loop" out of the one function whose docstring promises
#: idempotency under concurrency.
_gates: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def _gate() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = asyncio.Lock()
    return gate


def translate(exc: BaseException, *, connecting: bool = False) -> HiveDataError:
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
    if connecting and isinstance(exc, asyncio.TimeoutError):
        # `TimeoutError` is `asyncio.TimeoutError` and is a subclass of
        # `OSError`, so the class alone cannot say whether a host never
        # answered or a statement ran long. At connect time it is always the
        # former - and a dead host reported as a timeout asserts the
        # database accepted work it never received, sending the operator to
        # the database instead of to the host.
        return Unreachable(
            "no answer from the database inside the connect deadline: "
            f"{exc or 'the host did not respond'}")
    if isinstance(exc, asyncpg.InvalidPasswordError):
        return CredentialRefused(str(exc))
    if isinstance(exc, asyncpg.InvalidAuthorizationSpecificationError):
        return CredentialRefused(str(exc))
    if isinstance(exc, asyncpg.InvalidCatalogNameError):
        return DatabaseMissing(str(exc))
    if isinstance(exc, (asyncpg.InvalidSchemaNameError,
                        asyncpg.UndefinedTableError,
                        asyncpg.UndefinedColumnError)):
        # The database is not the one these operations are written against.
        # Before the generic branch below, which would call a missing schema
        # a refused write and send somebody to read a statement that is
        # fine. The common cause is real and benign: an install that runs
        # comms and not lucent has no `memory` schema, so the first memory
        # operation it calls should say that rather than imply a constraint.
        return SchemaMismatch(str(exc) or exc.__class__.__name__)
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
    if isinstance(exc, asyncpg.InFailedSQLTransactionError):
        return TransactionAborted(str(exc))
    if isinstance(exc, asyncpg.TooManyConnectionsError):
        # The database is up and answering everybody else. Reporting this as
        # unreachable sends somebody to a healthy server; reporting it as a
        # rejected write blames a statement nobody read.
        return NoConnectionAvailable(str(exc))
    if isinstance(exc, (asyncpg.PostgresConnectionError,
                        asyncpg.OperatorInterventionError)):
        # The server went away under us: a restart, an administrative
        # shutdown, a crash, a connection closed mid-statement. Before the
        # generic `PostgresError` branch below, which would otherwise call a
        # Postgres restart a refused write.
        return Unreachable(str(exc) or exc.__class__.__name__)
    if isinstance(exc, asyncpg.IntegrityConstraintViolationError):
        return WriteRejected(str(exc))
    if isinstance(exc, asyncio.TimeoutError):
        # Reached only outside the connect phase, where `connecting=True`
        # answers first. A timeout here is a statement the database took
        # and did not finish.
        return TimedOut("the database did not answer inside the deadline")
    if isinstance(exc, (OSError, socket.gaierror,
                        asyncpg.CannotConnectNowError)):
        # A refused connection, a name that does not resolve, a host that is
        # not there.
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
    global _loop
    resolved = settings or from_environment()
    running = asyncio.get_running_loop()
    async with _gate():
        if _pool is not None and _loop is not None and _loop is not running:
            raise HiveDataError(
                "a pool is already open on another event loop. asyncpg ties "
                "a pool to the loop that created it, so this one would fail "
                "on its first statement with a closed-loop error far from "
                "the call that reused it. Close the pool before the loop "
                "that opened it goes away.")
        if _pool is not None:
            if _settings is not None and _settings != resolved:
                if _settings.dsn != resolved.dsn:
                    raise HiveDataError(
                        "a pool is already open against "
                        f"{_settings.describes}; close it before opening "
                        f"one against {resolved.describes}. Two pools in "
                        "one process would let two operations in one "
                        "request answer from two databases.")
                # Same database, different numbers. Said out loud rather
                # than ignored: a second call asking for a larger pool is
                # somebody expecting a larger pool, and getting the first
                # call's size silently is how a service runs all year
                # against a ceiling nobody set.
                log.warning(
                    "hive-data: a pool is already open against %s with "
                    "%d-%d connections; keeping it. The settings this call "
                    "asked for (%d-%d) are not applied — close the pool "
                    "and open it again to change them.",
                    _settings.describes, _settings.pool_min,
                    _settings.pool_max, resolved.pool_min, resolved.pool_max)
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
                init=_register_codecs,
            )
        except BaseException as exc:
            _pool = None
            _settings = None
            raise translate(exc, connecting=True) from exc
        _settings = resolved
        _loop = running
    # At WARNING, deliberately. The one line whose job is to catch "this
    # service is pointed at a half-loaded database" is worthless at INFO:
    # nothing in the hive configures a handler for this logger, so an INFO
    # line is never printed by anybody.
    log.warning(
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
    global _pool, _settings, _loop
    async with _gate():
        pool, settings = _pool, _settings
        _pool = None
        _settings = None
        _loop = None
    if pool is None:
        return
    if drain_timeout is not None:
        deadline = drain_timeout
    elif settings is not None:
        deadline = settings.transaction_timeout
    else:
        # Never reached today - `_pool` and `_settings` are only ever set
        # together inside the gate - but written out so a later edit cannot
        # reach the unbounded wait this deadline exists to prevent, nor
        # format None into the warning below.
        deadline = DEFAULT_DRAIN_TIMEOUT
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
