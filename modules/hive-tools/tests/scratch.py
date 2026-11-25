"""The suite's scratch Postgres, its one event loop, and raw SQL against it.

The store is the hive's Postgres and nothing else, and a `hive_data` pool
belongs to the event loop that opened it. So every coroutine this suite
awaits — fixture setup, every request `TestClient` makes and its lifespan,
every coroutine test — runs on one loop, the suite's, held in a thread of
its own for the session.

The database is a scratch one, created and claimed for the run through
`hive_data.testing`, brought to the schema by `hive_data.migrate` exactly as
an install is, and with every `core` and `tools` table emptied before each
test. Never the live database: every DSN this
module touches is checked to be a scratch database first.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar
from urllib.parse import urlsplit

import anyio.from_thread

T = TypeVar("T")

ADMIN_DSN = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()

NO_SERVER = (
    "HIVE_DATA_TEST_ADMIN_DSN is unset. It names a Postgres this suite may "
    "create and drop a scratch database on; hive-tools' store is that "
    "Postgres and nothing else, so without it there is nothing to test "
    "against."
)

#: The name every scratch database here starts with, one `hive_data.testing`
#: sweeps and claims.
SCRATCH_PREFIX = "hive_data_test_"

#: The schemas emptied before each test. The scratch database holds every
#: schema `hive_data.migrate` applies; these are the two hive-tools' tests
#: write - `core` because the access layer refuses a database without it.
_SCHEMA_NAMES = ("core", "tools")


def refuse_unless_scratch(dsn: str) -> str:
    """The DSN, if it names a scratch database; otherwise refuse."""
    name = urlsplit(dsn).path.lstrip("/")
    if not name.startswith(SCRATCH_PREFIX):
        raise RuntimeError(
            f"refusing database {name!r}: a test may only touch a scratch "
            f"database, whose name starts {SCRATCH_PREFIX!r}.")
    return dsn


# ── the suite's one event loop ──────────────────────────────────────────

_start_blocking_portal = anyio.from_thread.start_blocking_portal
_portal: Any = None
_portal_scope: Any = None


def suite_portal() -> Any:
    global _portal, _portal_scope
    if _portal is None:
        _portal_scope = _start_blocking_portal(backend="asyncio")
        _portal = _portal_scope.__enter__()
    return _portal


def stop_suite_portal() -> None:
    global _portal, _portal_scope
    scope, _portal_scope, _portal = _portal_scope, None, None
    if scope is not None:
        scope.__exit__(None, None, None)


async def _awaiting(work: Coroutine[Any, Any, T]) -> T:
    return await work


def run(work: Coroutine[Any, Any, T]) -> T:
    """Await one coroutine on the suite's loop, from a synchronous test."""
    return suite_portal().call(_awaiting, work)


@contextlib.contextmanager
def _the_suite_portal(**_options: Any) -> Iterator[Any]:
    yield suite_portal()


@contextlib.contextmanager
def testclients_on_the_suite_loop() -> Iterator[None]:
    """Every `TestClient` request, and its lifespan, on the suite's loop."""
    real = anyio.from_thread.start_blocking_portal
    anyio.from_thread.start_blocking_portal = _the_suite_portal
    try:
        yield
    finally:
        anyio.from_thread.start_blocking_portal = real


# ── the scratch database ────────────────────────────────────────────────

class ScratchDatabase:
    """A claimed scratch database with the schemas applied."""

    def __init__(self, claimed: Any) -> None:
        self._claimed = claimed
        self.dsn = refuse_unless_scratch(claimed.dsn)

    @classmethod
    async def create(cls) -> ScratchDatabase:
        import asyncpg
        from hive_data import migrate
        from hive_data.testing import open_claimed, sweep_stale

        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await sweep_stale(admin)
        finally:
            await admin.close()
        claimed = await open_claimed(ADMIN_DSN, SCRATCH_PREFIX)
        try:
            scratch = cls(claimed)
            # Through the migration, as an install is, so a step added
            # after the baseline - a column hive-tools reads - is here too.
            await migrate.apply(scratch.dsn)
        except BaseException:
            await claimed.close()
            raise
        return scratch

    async def reset(self) -> None:
        """Empty every table, so each test starts on an empty store."""
        import asyncpg

        connection = await asyncpg.connect(refuse_unless_scratch(self.dsn))
        try:
            name = await connection.fetchval("SELECT current_database()")
            if not str(name).startswith(SCRATCH_PREFIX):
                raise RuntimeError(
                    f"refusing to empty {name!r}: not a scratch database.")
            tables = await connection.fetch(
                "SELECT format('%I.%I', schemaname, tablename) AS name "
                "FROM pg_tables WHERE schemaname = ANY($1::text[])",
                list(_SCHEMA_NAMES))
            if tables:
                await connection.execute(
                    "TRUNCATE " + ", ".join(row["name"] for row in tables)
                    + " RESTART IDENTITY CASCADE")
        finally:
            await connection.close()

    async def close(self) -> None:
        await self._claimed.close()


# ── raw SQL, for a test setting up or reading back a row directly ───────

class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)


def _numbered(sql: str) -> str:
    """`?` placeholders as Postgres's `$1`, `$2`, … ."""
    count = 0

    def number(_match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"${count}"

    return re.sub(r"\?", number, sql)


class RawSQL:
    """A statement against the scratch database, outside the store.

    Its own connection rather than the store's pool, so what a test reads
    back is what landed rather than what one layer said it wrote, and so it
    can be called from a synchronous test and a coroutine test alike.
    Unqualified table names resolve in `tools`.
    """

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> _Result:
        # On a loop and a thread of its own: a connection of its own needs no
        # particular loop, and the caller may be a coroutine test already
        # running on the suite's.
        with ThreadPoolExecutor(max_workers=1) as worker:
            return worker.submit(
                asyncio.run, self._execute(_numbered(sql), tuple(args))
            ).result()

    def commit(self) -> None:
        """Every statement has already landed; kept for the call sites."""

    async def _execute(self, sql: str, args: tuple[Any, ...]) -> _Result:
        import asyncpg

        connection = await asyncpg.connect(
            refuse_unless_scratch(os.environ["HIVE_DATABASE_URL"]),
            server_settings={"search_path": "tools"})
        try:
            return _Result(list(await connection.fetch(sql, *args)))
        finally:
            await connection.close()


db = RawSQL()
