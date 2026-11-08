"""The broker half's tests run against a real Postgres of their own.

The broker, the surface claims, the credential gate and the inter-mind API
read and write the `core` schema through `hive_data` and open nothing
themselves, so their tests need what production has: a database with
`core.sql` applied and a pool open on it. The database is the one
`lucent_api.tests.conftest.scratch_dsn` claims for the run — imported, not
reimplemented: one sweep and one allow-list across every suite, or one
suite's sweep drops another's database out from under it. Never the live
`hive` database.

Two shapes, because the tests come in two:

* `broker_store` — async. The pool is opened on the test's own loop and
  closed after it. For tests that call the modules directly, or drive an app
  over `httpx.ASGITransport` (no lifespan, the fixture owns the pool).
* `broker_db` — sync. Names the database in the environment and nothing
  else, for a `TestClient` whose app opens the pool in its own lifespan on
  its own loop: asyncpg ties a pool to the loop that opened it.

Both empty every table the broker half writes before the test, minds
included — a scratch database outlives the test that wrote it, and
`core.minds.name` is a primary key, so two tests registering "alpha" under
different ids collide in setup. The truncate cascades, which on a scratch
database is the point: the sessions naming those minds go with them.

What a test reads back to check a write is read on a plain driver connection
(`fetch`, `fetchval`, `execute`), never through the code under test: a layer
that writes and reads the same wrong column passes its own round trip.

Use it from a test module:

    from comms_broker_pg import broker_store, scratch_dsn  # noqa: F401
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

import hive_data

from lucent_api.tests.conftest import scratch_dsn  # noqa: F401 - re-exported fixture

#: Every table the broker half writes, and those that hold a key into them.
_TABLES = (
    "core.minds", "core.credentials", "core.secret_scopes", "core.settings",
    "core.messages", "core.conversations", "core.surface_claims",
    "core.sessions", "core.active_sessions", "core.session_turns",
    "core.session_memory", "core.context_overhead",
)


async def wipe(dsn: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f"TRUNCATE {', '.join(_TABLES)} CASCADE")
    finally:
        await connection.close()


@pytest.fixture
def broker_db(scratch_dsn, monkeypatch):  # noqa: F811 - the imported fixture
    """The scratch database, emptied and named in the environment."""
    monkeypatch.setenv(hive_data.config.DSN_VAR, scratch_dsn)
    asyncio.run(wipe(scratch_dsn))
    return scratch_dsn


@pytest_asyncio.fixture
async def broker_store(scratch_dsn, monkeypatch):  # noqa: F811
    """The scratch database, emptied, with a pool open on this test's loop."""
    monkeypatch.setenv(hive_data.config.DSN_VAR, scratch_dsn)
    await wipe(scratch_dsn)
    await hive_data.connect()
    try:
        yield scratch_dsn
    finally:
        await hive_data.close(drain_timeout=5.0)


def _dsn() -> str:
    return os.environ[hive_data.config.DSN_VAR]


async def execute(sql: str, *args: Any) -> str:
    connection = await asyncpg.connect(_dsn())
    try:
        return await connection.execute(sql, *args)
    finally:
        await connection.close()


async def fetch(sql: str, *args: Any) -> list[dict]:
    connection = await asyncpg.connect(_dsn())
    try:
        return [dict(row) for row in await connection.fetch(sql, *args)]
    finally:
        await connection.close()


async def fetchval(sql: str, *args: Any) -> Any:
    rows = await fetch(sql, *args)
    return None if not rows else next(iter(rows[0].values()))


async def put_mind(
    mind_id: str, name: str | None = None, *, owner: str | None = None,
    model: str = "sonnet", harness: str = "claude_cli",
    gateway_url: str = "http://mind.invalid:8420",
    session_token: str | None = None, voice: str | None = None,
    voice_engine: str | None = None,
) -> str:
    """A mind row written straight to the table, as setup rather than subject."""
    now = time.time()
    await execute(
        "INSERT INTO core.minds (mind_id, name, gateway_url, model, harness, "
        "registered_at, last_seen, session_token, voice, voice_engine, owner) "
        "VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8, $9, $10)",
        mind_id, name or mind_id, gateway_url, model, harness, now,
        session_token, voice, voice_engine, owner,
    )
    return mind_id


async def put_credential_row(
    holder: str, username: str, role: str, token_sha: str,
    revoked_at: float | None = None,
) -> None:
    """A credential row as setup. The digest is the caller's to compute —
    from `user_access.digest`, so a change to how a value is stored fails the
    test rather than passing against a hash this file computed its own way."""
    await execute(
        "INSERT INTO core.credentials "
        "(holder, username, role, token_sha, created_at, revoked_at) "
        "VALUES ($1, $2, $3, $4, 0, $5)",
        holder, username, role, token_sha, revoked_at,
    )
