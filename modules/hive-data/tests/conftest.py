"""A database of this suite's own, made and dropped per run.

Never the live `hive` database. The rows there are the pre-cutover copies the
loader story still has to verify row for row, and writing test sessions,
minds and credentials into them would poison exactly the comparison that is
supposed to prove the migration correct. `core.sessions.mind_id` is also
ON DELETE RESTRICT, so cleaning up afterwards is order-dependent and one
failed teardown leaves the live schema dirty.

The database is *claimed* for the length of the run — see `tests/_scratch.py`
for why a concurrent run would otherwise drop it out from under this one.

The schema comes from `hive_data/schema/core.sql`, a captured `pg_dump -s` of
the applied `core` schema, vendored into the package. The generator that
produced it reads the seven live SQLite stores, so it cannot be run from
here — and a suite that depended on another checkout and on live stores
being present would be a suite that fails on any other machine.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

import hive_data

from tests._scratch import claimed_database, sweep_stale

#: Where the server is. Named in the environment with no default, because a
#: default would be a credential in a tracked file.
#:
#: Point it at the server over TCP rather than at a loopback socket, and the
#: reason is load-bearing for one test: the hive's `pg_hba.conf` trusts
#: loopback and demands scram from everywhere else, so a wrong password over
#: loopback *succeeds* and the refused-credential test would pass for a
#: reason that has nothing to do with this code.
ADMIN_DSN = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()

#: Said once, where a reader will look for it, rather than per test.
NO_SERVER = (
    "HIVE_DATA_TEST_ADMIN_DSN is unset. It names a Postgres this suite may "
    "create and drop a database on, reached over TCP rather than a loopback "
    "socket. There is deliberately no default: a default would be a "
    "credential in a tracked file."
)

_SCHEMA_DIR = Path(hive_data.__file__).parent / "schema"
SCHEMA_SQL = _SCHEMA_DIR / "core.sql"
#: Applied beside `core.sql`. A separate artifact because a deployment can
#: simply not create a context it does not run — comms needs `core` and
#: nothing else — and because this one needs the `vector` extension, which
#: is not a trusted extension and so needs a superuser to install.
MEMORY_SCHEMA_SQL = _SCHEMA_DIR / "memory.sql"
#: Applied beside the other two, for the same reason they are separate
#: artifacts: a deployment creates the contexts it runs and no others.
#: hive-tools needs this one and nothing else needs it at all.
TOOLS_SCHEMA_SQL = _SCHEMA_DIR / "tools.sql"


@pytest_asyncio.fixture(scope="session")
async def scratch_dsn() -> str:
    if not ADMIN_DSN:
        pytest.skip(NO_SERVER)
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        # A run that is killed never reaches the teardown below, so it
        # leaves its database behind — one per interrupted run, for ever.
        # Swept here rather than left to somebody noticing: the names are
        # this suite's own and nothing else creates them. Only the ones no
        # live run has claimed.
        await sweep_stale(admin)
    finally:
        await admin.close()
    async with claimed_database(ADMIN_DSN, "hive_data_test_") as dsn:
        target = await asyncpg.connect(dsn)
        try:
            await target.execute(SCHEMA_SQL.read_text())
            await target.execute(MEMORY_SCHEMA_SQL.read_text())
            await target.execute(TOOLS_SCHEMA_SQL.read_text())
        finally:
            await target.close()
        yield dsn


@pytest_asyncio.fixture
async def connected(scratch_dsn, monkeypatch):
    """An open pool against the scratch database, closed afterwards."""
    monkeypatch.setenv(hive_data.config.DSN_VAR, scratch_dsn)
    await hive_data.connect()
    try:
        yield scratch_dsn
    finally:
        await hive_data.close(drain_timeout=5.0)


@pytest_asyncio.fixture
async def inspector(scratch_dsn):
    """A connection outside the package, for checking what actually landed.

    Deliberately not one of the package's own. A round trip through one layer
    passes even when every operation writes and reads the same wrong column,
    so what a write produced is read back with a plain driver connection and
    plain SQL.
    """
    connection = await asyncpg.connect(scratch_dsn)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def clock():
    """A fixed moment, so nothing in a test depends on when it ran."""
    return 1_800_000_000.0


@pytest_asyncio.fixture
async def a_mind(connected, clock):
    """A mind row, because a session cannot exist without one.

    `core.sessions.mind_id` is a real foreign key now — it could never be one
    while each store was its own SQLite file.
    """
    mind_id = f"mind-{uuid.uuid4().hex[:8]}"
    await hive_data.core.upsert_mind(
        mind_id=mind_id, name=f"name-{mind_id}",
        gateway_url="http://127.0.0.1:8426", model="claude-opus-5",
        harness="claude", registered_at=clock, last_seen=clock)
    return mind_id
