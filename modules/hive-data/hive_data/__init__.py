"""The hive's data-access layer: one async package every service goes through.

No service opens a database. A service calls a named operation, and where the
database actually lives is one configuration value in that process's
environment — so pointing the hive at a different database is an environment
change rather than an edit to anything.

Async only. There is no synchronous face and no threadpool bridge to write.
Two faces would mean every query existing twice forever, and the sync one
silently blocking somebody's event loop the first time a service forgot the
bridge. Work that is genuinely CPU-bound — a similarity scan over vectors —
belongs in `asyncio.to_thread` whatever the database is.

Open once at startup and close at shutdown:

    import hive_data

    await hive_data.connect()
    ...
    await hive_data.close()

Nothing opens a pool on demand. A lazy open under two concurrent first calls
builds two pools and holds double the connections against a ceiling shared
with every other service, with nothing reporting that it happened — so the
first operation against an unopened package is an error that says so.
"""

from __future__ import annotations

from . import board
from . import core
from .config import Settings, from_environment
from .errors import (
    Conflict,
    StaleRead,
    CredentialRefused,
    DatabaseMissing,
    HiveDataError,
    IdentityNotWritable,
    MissingReference,
    NoConnectionAvailable,
    NotConfigured,
    NotInitialised,
    SchemaMismatch,
    SequenceBehind,
    TimedOut,
    TransactionAborted,
    TransactionMisuse,
    Unreachable,
    UnsettableColumn,
    ValueRefused,
    WriteRejected,
)
from . import graph
from . import memory
from . import staff
from . import tools
from .pool import close_pool, live_pool, open_pool, run, settings_in_use
from .rows import UNSET
from .schema import COLUMNS
from .unit import Unit, conversation_lock, transaction

__all__ = [
    "COLUMNS", "Conflict", "StaleRead", "CredentialRefused", "DatabaseMissing",
    "HiveDataError", "IdentityNotWritable", "MissingReference",
    "NoConnectionAvailable", "NotConfigured", "NotInitialised",
    "SchemaMismatch", "SequenceBehind", "Settings", "TimedOut",
    "TransactionAborted", "TransactionMisuse", "UNSET",
    "Unit", "Unreachable", "UnsettableColumn", "ValueRefused",
    "WriteRejected", "board", "close", "connect", "conversation_lock",
    "core",
    "from_environment", "graph",
    "live_pool", "memory", "run", "settings_in_use", "staff", "tools",
    "transaction",
    "verify_schema",
]


async def connect(settings: Settings | None = None, *,
                  verify: bool = True) -> None:
    """Open the pool and check the schema it is pointed at.

    The check is on by default and runs here rather than at the first call
    site, so a database that is not the one these operations were written
    against is a process that refuses to start. A column this package names
    and the database does not have would otherwise arrive as a missing dict
    key — and the first consumer reads one of those as a value.
    """
    pool = await open_pool(settings)
    if verify:
        try:
            await verify_schema()
        except BaseException:
            await close_pool()
            raise
    return None


async def verify_schema() -> None:
    """Check the applied schema against the columns these operations name."""
    from .schema import verify

    await run(verify)


async def close(drain_timeout: float | None = None) -> None:
    """Close the pool, waiting for work in flight up to a deadline."""
    await close_pool(drain_timeout)
