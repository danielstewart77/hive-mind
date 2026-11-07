"""A database of this suite's own, made and dropped per run.

Never the live `hive` database. The rows there are what the cutover is
verified against, and writing test credentials and test Mind nodes into them
poisons exactly the comparison that is supposed to prove the move correct.

The claim machinery is `hive_data.testing`, not a copy of it. One sweep and
one allow-list across every suite that needs a scratch database: a second
implementation with its own prefixes is how one suite's sweep drops another
suite's database out from under it, which has already cost nine runs in seven
minutes once.

Every test here gets a fresh schema rather than a fresh database — the
database is session-scoped and dropping it per test would be a `CREATE` and
four schema applications per test. What a test must not do is assume an empty
table: `memory_nodes_owner_type_name` is a real unique index now, so two
tests seeding the same owner, type and name collide in setup where each used
to get a virgin in-memory store. Name things with `unique()`.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

import hive_data
from hive_data.testing import claimed_database, sweep_stale

#: Where the server is. Named in the environment with no default, because a
#: default would be a credential in a tracked file. The suite skips cleanly
#: without it rather than failing, and says why.
ADMIN_DSN = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()

NO_SERVER = (
    "HIVE_DATA_TEST_ADMIN_DSN is unset. It names a Postgres this suite may "
    "create and drop a database on. There is deliberately no default: a "
    "default would be a credential in a tracked file."
)

_SCHEMA_DIR = Path(hive_data.__file__).parent / "schema"


def unique(prefix: str = "x") -> str:
    """A name no other test in this run will also write.

    Not a convenience. Nine of this suite's classes used to build their own
    empty in-memory database in `setUp`, so a fixed `('daniel', 'Person',
    'Maurice')` was fresh every time; against one Postgres with a unique
    index on that triple, the second test to seed it fails in setup.
    """
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest_asyncio.fixture(scope="session")
async def scratch_dsn() -> str:
    if not ADMIN_DSN:
        pytest.skip(NO_SERVER)
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await sweep_stale(admin)
    finally:
        await admin.close()
    async with claimed_database(ADMIN_DSN, "lucent_test_") as dsn:
        target = await asyncpg.connect(dsn)
        try:
            # `core` first: `memory.edges.mind_id` is a real foreign key into
            # `core.minds`, which is the kind of thing that could not exist
            # while each store was its own file.
            await target.execute((_SCHEMA_DIR / "core.sql").read_text())
            await target.execute((_SCHEMA_DIR / "memory.sql").read_text())
        finally:
            await target.close()
        yield dsn


@pytest_asyncio.fixture
async def store(scratch_dsn, monkeypatch):
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

    A round trip through one layer passes even when the write and the read
    agree on the same wrong column, so what a route produced is read back
    with a plain driver connection and plain SQL.
    """
    connection = await asyncpg.connect(scratch_dsn)
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def registered_types(store):
    """The node types these tests write, registered.

    `memory.nodes.type` references `memory.schema_types` ON DELETE RESTRICT.
    SQLite never enforced a foreign key here — lucent's connection never set
    the pragma — so every test could invent a type. Now a database that is
    not the final load refuses every write with `Must be one of: []` until
    the registry is seeded, which is also true of a fresh install and is why
    the seed is part of the service rather than part of the load.
    """
    from lucent_api import schema_registry

    await schema_registry.seed_schema_tables()
    return True


@pytest_asyncio.fixture
async def a_mind(store):
    """A registered mind, because `memory.edges.mind_id` points at one."""
    mind_id = unique("mind")
    await hive_data.core.upsert_mind(
        mind_id=mind_id, name=mind_id,
        gateway_url="http://127.0.0.1:8426", model="claude-opus-5",
        harness="claude", registered_at=1_800_000_000.0,
        last_seen=1_800_000_000.0)
    return mind_id
