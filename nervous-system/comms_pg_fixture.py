"""The session manager's tests run against a real Postgres of their own.

The session manager reads and writes the `core` schema through `hive_data`
and opens nothing itself, so its tests need what production has: a database
with `core.sql` applied and a pool open on it. The database is the one
`lucent_api.tests.conftest.scratch_dsn` claims for the run — imported, not
reimplemented, for the reason that module gives: one sweep and one
allow-list across every suite, or one suite's sweep drops another's database
out from under it.

Most of these tests are synchronous and drive their scenario through
`asyncio.run`, one event loop per test. asyncpg ties a pool to the loop that
opened it, so the pool is opened inside the scenario (`make_manager`) and
closed before the loop goes (`close_manager`). The `comms_db` fixture empties
the session tables before each test and points `HIVE_DATABASE_URL` at the
scratch database; it never touches the live `hive` database.

What a test reads back to check a write is read on a plain driver connection
(`fetch`, `fetchval`), not through the code under test: a layer that writes
and reads the same wrong column passes its own round trip.

Use it from a test module:

    from comms_pg_fixture import comms_db, scratch_dsn  # noqa: F401
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import asyncpg
import pytest

import hive_data
from hive_data import core

from lucent_api.tests.conftest import scratch_dsn  # noqa: F401 - re-exported fixture

#: What a session test may leave behind, emptied before the next one. Minds
#: are not in it: `core.minds` is shared with lucent's suite in the same run,
#: and a mind is registered by an idempotent upsert anyway.
_SESSION_TABLES = (
    "core.session_turns", "core.active_sessions", "core.session_memory",
    "core.context_overhead", "core.sessions",
)


async def _wipe(dsn: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f"TRUNCATE {', '.join(_SESSION_TABLES)}")
    finally:
        await connection.close()


@pytest.fixture
def comms_db(scratch_dsn, monkeypatch):  # noqa: F811 - the imported fixture
    """The scratch database, its session tables empty, named in the env."""
    monkeypatch.setenv(hive_data.config.DSN_VAR, scratch_dsn)
    asyncio.run(_wipe(scratch_dsn))
    return scratch_dsn


def _dsn() -> str:
    return os.environ[hive_data.config.DSN_VAR]


async def make_manager(*, start: bool = True):
    """Open the pool on this loop and hand back a started session manager."""
    from comms.sessions import SessionManager

    await hive_data.connect()
    mgr = SessionManager()
    if start:
        await mgr.start()
    return mgr


async def close_manager(mgr) -> None:
    """Shut the manager down and close the pool before this loop ends."""
    try:
        await mgr.shutdown()
    finally:
        await hive_data.close(drain_timeout=5.0)


async def register_mind(
    mind_id: str = "ada", *, name: str | None = None, model: str = "sonnet",
    harness: str = "claude_cli", gateway_url: str = "http://mind.invalid:8420",
    session_token: str | None = None,
) -> str:
    """A mind registration, because `sessions.mind_id` is a real foreign key."""
    now = time.time()
    kwargs: dict[str, Any] = {}
    if session_token is not None:
        kwargs["session_token"] = session_token
    await core.upsert_mind(
        mind_id=mind_id, name=name or mind_id, gateway_url=gateway_url,
        model=model, harness=harness, registered_at=now, last_seen=now,
        **kwargs,
    )
    return mind_id


async def seed_session(
    session_id: str = "sess-1", *, surface: str = "telegram",
    surface_id: str = "123", mind_id: str = "ada", status: str = "running",
    bind: tuple[str, str] | None = None, **columns: Any,
) -> str:
    """A session row written straight to the table.

    Registers its mind if nothing has. ``bind`` adds an `active_sessions`
    row as ``(client_type, client_ref)``. Any other column is given under
    its stored name.
    """
    if await core.get_mind(mind_id) is None:
        await register_mind(mind_id)
    now = time.time()
    values: dict[str, Any] = {
        "created_at": now, "last_active": now, "conversation_id": "conv-1",
        "model": "opus",
    }
    for name, value in columns.items():
        values[name] = value
    if "terminal_held" in values and values["terminal_held"] is not None:
        values["terminal_held"] = bool(values["terminal_held"])
    if values.get("context_has_images") is not None:
        values["context_has_images"] = bool(values["context_has_images"])
    names = ["id", "surface", "surface_id", "mind_id", "status", *values]
    args = [session_id, surface, surface_id, mind_id, status, *values.values()]
    placeholders = ", ".join(f"${i}" for i in range(1, len(args) + 1))
    await execute(
        f"INSERT INTO core.sessions ({', '.join(names)}) VALUES ({placeholders})",
        *args,
    )
    if bind is not None:
        await execute(
            "INSERT INTO core.active_sessions (client_type, client_ref, session_id) "
            "VALUES ($1, $2, $3)", bind[0], bind[1], session_id,
        )
    return session_id


async def add_turn(session_id: str, role: str, content: str,
                   created_at: float | None = None) -> None:
    await execute(
        "INSERT INTO core.session_turns (session_id, role, content, created_at) "
        "VALUES ($1, $2, $3, $4)",
        session_id, role, content, time.time() if created_at is None else created_at,
    )


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


async def fetchrow(sql: str, *args: Any) -> dict | None:
    rows = await fetch(sql, *args)
    return rows[0] if rows else None


async def fetchval(sql: str, *args: Any) -> Any:
    row = await fetchrow(sql, *args)
    return None if row is None else next(iter(row.values()))
