"""Run a site's suite against a scratch Postgres, on one event loop.

The store is the hive's Postgres and nothing else, and a `hive_data` pool
belongs to the event loop that opened it. Most of both sites' suites are
synchronous: they drive a site through `TestClient`, which would otherwise
start a loop of its own for every request, and they set up accounts from
plain test functions. So a suite runs every coroutine it awaits — its own
setup, every request `TestClient` makes, every coroutine test — on one loop,
the suite's, held in a thread of its own for the whole session.

The database is a scratch one, created and claimed for the run through
`hive_data.testing`, with the hive's schemas applied and every table emptied
before each test. Never the live database: every DSN this module hands out or
empties is checked to be a scratch database first, and anything else is
refused.

It ships inside the package rather than in either suite because both suites
need it and a copy in each is how `auth.py` got into the state this package
was made to end.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import anyio.from_thread

from . import auth

T = TypeVar("T")

#: A Postgres this suite may create and drop a database on. Named in the
#: environment with no default, because a default would be a credential in a
#: tracked file.
ADMIN_DSN = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()

NO_SERVER = (
    "HIVE_DATA_TEST_ADMIN_DSN is unset. It names a Postgres this suite may "
    "create and drop a scratch database on; the site's store is that "
    "Postgres and nothing else, so without it there is nothing to test "
    "against."
)

#: The name every scratch database here starts with. One `hive_data.testing`
#: sweeps and claims, so a killed run's leftover is dropped by the next run
#: and a live run's database is never dropped out from under it.
SCRATCH_PREFIX = "hive_data_test_"

#: The schemas to apply, as applied, vendored in the access layer. Read from
#: the installed package rather than copied here, so a schema change arrives
#: instead of being silently out of date. `core` before `board`: `board`
#: carries real foreign keys into `core.sessions` and `core.minds`.
_SCHEMA_ORDER = ("core.sql", "memory.sql", "board.sql")

_SCHEMA_NAMES = ("core", "memory", "board")


def schemas() -> list[str]:
    """The schema statements to apply to a scratch database, in order."""
    import hive_data

    directory = Path(hive_data.__file__).parent / "schema"
    return [(directory / name).read_text() for name in _SCHEMA_ORDER]


def refuse_unless_scratch(dsn: str) -> str:
    """The DSN, if it names a scratch database; otherwise refuse.

    The one check between a test suite and the live `hive` database. A
    suite that wrote test accounts and test boards into it would not fail —
    it would edit production and report green.
    """
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
    """The suite's loop, started on first use and kept for the session."""
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
    """Every `TestClient` request, and its lifespan, on the suite's loop.

    `TestClient` starts a fresh loop per request when it is not entered, and
    one per `with` block when it is. Either would be a loop the store's pool
    does not belong to.
    """
    real = anyio.from_thread.start_blocking_portal
    anyio.from_thread.start_blocking_portal = _the_suite_portal
    try:
        yield
    finally:
        anyio.from_thread.start_blocking_portal = real


# ── the scratch database ────────────────────────────────────────────────

class ScratchDatabase:
    """A claimed scratch database with the hive's schemas applied."""

    def __init__(self, claimed: Any) -> None:
        self._claimed = claimed
        self.dsn = refuse_unless_scratch(claimed.dsn)

    @classmethod
    async def create(cls) -> ScratchDatabase:
        import asyncpg
        from hive_data.testing import open_claimed, sweep_stale

        if not ADMIN_DSN:
            raise RuntimeError(NO_SERVER)
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            # A killed run never reaches its teardown and leaves its
            # database behind; the ones nobody has claimed are dropped here.
            await sweep_stale(admin)
        finally:
            await admin.close()
        claimed = await open_claimed(ADMIN_DSN, SCRATCH_PREFIX)
        try:
            scratch = cls(claimed)
            target = await asyncpg.connect(scratch.dsn)
            try:
                for schema in schemas():
                    await target.execute(schema)
            finally:
                await target.close()
        except BaseException:
            await claimed.close()
            raise
        return scratch

    async def reset(self) -> None:
        """Empty every table, so each test starts on an empty hive."""
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


# ── rows another service owns ───────────────────────────────────────────

async def make_conversations(*ids: str) -> None:
    """Gateway session rows for conversations a test points a row at.

    `board.design_sessions.conversation_id` is a real foreign key into
    `core.sessions`, which comms writes; a test naming a conversation has to
    make the row first, under a mind of its own.
    """
    from hive_data import core

    mind_id = "test-mind"
    await core.upsert_mind(mind_id=mind_id, name="test-mind",
                           gateway_url="http://test-mind:8421",
                           model="a-model", harness="claude",
                           registered_at=1.0, last_seen=1.0, owner="daniel")
    for conversation_id in ids:
        await core.create_session(
            id=conversation_id, conversation_id=conversation_id,
            surface="console", surface_id="tile", model="a-model",
            created_at=1.0, last_active=1.0, mind_id=mind_id)


# ── accounts, from a synchronous test ───────────────────────────────────

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
