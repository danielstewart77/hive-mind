"""Set up an account, or drive the store, from a synchronous test.

The account store is async, and most of both sites' suites are synchronous:
they drive a site through its own HTTP surface, which `TestClient` runs on a
loop of its own. These helpers run one coroutine on a loop of their own so a
test can go on reading as the setup step it is.

Safe on the SQLite half and nowhere else, which is the half both suites run
on. There the store is a connection opened, used and closed inside a worker
thread, reached through an executor rather than through anything bound to a
particular event loop, so a write made from this loop is a write the next
request sees. The Postgres half's pool is bound to the loop that opened it,
which is exactly why no production path does this.

It ships inside the package rather than in either suite because both suites
need it and a copy in each is how `auth.py` got into the state this package
was made to end.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from . import auth, store

T = TypeVar("T")


def run(work: Coroutine[Any, Any, T]) -> T:
    """Await one store operation from a synchronous test."""
    return asyncio.run(work)


def make_user(username: str, password: str, *, is_admin: bool = False,
              replace: bool = False) -> dict[str, Any]:
    """A built-in account with a settled password."""
    return run(auth.create_user(username, password, is_admin=is_admin,
                                replace=replace))


def session_token(user: dict[str, Any]) -> str:
    """A built-in session cookie value for an existing account."""
    return run(auth.create_session_token(user))


def seed_first_user() -> None:
    """Create the initial admin from the environment, as a lifespan does."""
    run(auth.seed_first_user())


# ── a scratch Postgres, for the half a file cannot cover ────────────────

ADMIN_DSN = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()

NO_SERVER = (
    "HIVE_DATA_TEST_ADMIN_DSN is unset. It names a Postgres this suite may "
    "create and drop a database on. Without it the two halves cannot be "
    "compared and these tests are skipped rather than passing on one half."
)

#: The schemas to apply to the scratch database, as applied, vendored in the
#: access layer. Read from the installed package rather than copied here, so
#: a schema change arrives instead of being silently out of date.
#:
#: `core.sql` as well as `board.sql`, and in that order: `board` carries real
#: foreign keys into `core.sessions` and `core.minds`, which is the first
#: thing in this database that could not exist while each store was its own
#: SQLite file. `memory.sql` too, for the browser — it needs the `vector`
#: extension, which is not a trusted one, so the scratch database is created
#: by the same credential `hive_data`'s own suite uses.
_SCHEMA_ORDER = ("core.sql", "memory.sql", "board.sql")


def schemas() -> list[str]:
    """The schema statements to apply to a scratch database, in order."""
    import hive_data

    directory = Path(hive_data.__file__).parent / "schema"
    return [(directory / name).read_text() for name in _SCHEMA_ORDER]


async def on_sqlite(sequence: Callable[[], Awaitable[Any]],
                     db_path: Path) -> Any:
    """Run a sequence against a temporary SQLite file."""
    os.environ[store.DSN_VAR] = ""
    await store.open(db_path)
    try:
        assert not store.on_postgres()
        return await sequence()
    finally:
        await store.close()


async def create_scratch_database(prefix: str = "hive_site") -> tuple[str, str]:
    """A fresh database on the named server with the schemas applied.

    Returns its name and its DSN. Pair with `drop_scratch_database`.
    """
    import asyncpg

    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    base, _, _ = ADMIN_DSN.rpartition("/")
    dsn = f"{base}/{name}"
    target = await asyncpg.connect(dsn)
    try:
        for schema in schemas():
            await target.execute(schema)
    finally:
        await target.close()
    return name, dsn


async def drop_scratch_database(name: str) -> None:
    import asyncpg

    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()", name)
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await admin.close()


async def on_postgres(sequence: Callable[[], Awaitable[Any]]) -> Any:
    """Run the same sequence against a scratch Postgres database."""
    name, dsn = await create_scratch_database("hive_data_parity")
    try:
        os.environ[store.DSN_VAR] = dsn
        await store.open()
        try:
            assert store.on_postgres()
            return await sequence()
        finally:
            await store.close()
    finally:
        os.environ[store.DSN_VAR] = ""
        await drop_scratch_database(name)


def both_halves(sequence: Callable[[], Awaitable[Any]],
                db_path: Path) -> tuple[Any, Any]:
    """What each half answered, for the same sequence of operations.

    Run in separate event loops, one after the other, because each half opens
    and closes its own store and the Postgres pool belongs to the loop it was
    opened on.
    """
    if not ADMIN_DSN:
        pytest.skip(NO_SERVER)
    on_file = asyncio.run(on_sqlite(sequence, db_path))
    on_server = asyncio.run(on_postgres(sequence))
    return on_file, on_server
