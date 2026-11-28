"""What the installer finds in an existing hive, on a throwaway database.

What landed is read back on a plain driver connection, never through the code
under test.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg

from comms_broker_pg import broker_db, put_mind, scratch_dsn  # noqa: F401 - fixtures
from installer import collisions, probe



async def _rows(dsn: str, sql: str, *args):
    conn = await asyncpg.connect(dsn)
    try:
        return [dict(r) for r in await conn.fetch(sql, *args)]
    finally:
        await conn.close()


def _rows_sync(dsn, sql, *args):
    return asyncio.run(_rows(dsn, sql, *args))


def _counts(dsn: str) -> dict:
    tables = ["core.users", "core.minds", "core.credentials", "memory.credentials"]
    return {t: _rows_sync(dsn, f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in tables}


def test_against_an_existing_hive_an_unknown_admin_or_a_taken_mind_name_is_refused_unwritten(
        broker_db):  # noqa: F811 - the imported fixture
    dsn = broker_db
    empty = asyncio.run(probe.gather(dsn))
    assert collisions.refusals(
        empty, admin="ann", mind_name="my-mind", mind_id=str(uuid.uuid4()),
        holders={"comms": ["hive-site"], "lucent": []}, ours={}) == []
    _rows_sync(dsn, "INSERT INTO core.users (username, password_hash, created_at) "
                    "VALUES ('daniel', 'x', 1) RETURNING id")
    asyncio.run(put_mind(str(uuid.uuid4()), "my_mind", owner="daniel"))
    before = _counts(dsn)

    facts = asyncio.run(probe.gather(dsn))
    refused = collisions.refusals(
        facts, admin="ann", mind_name="my-mind", mind_id=str(uuid.uuid4()),
        holders={"comms": ["hive-site"], "lucent": []}, ours={})

    assert any("'ann'" in r and "no account" in r for r in refused)
    assert any("'my_mind'" in r for r in refused)
    assert _counts(dsn) == before
    assert collisions.refusals(
        facts, admin="Daniel", mind_name="wren", mind_id=str(uuid.uuid4()),
        holders={"comms": ["hive-site"], "lucent": []}, ours={}) == []
