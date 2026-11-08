# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
"""Deleting a mind waits on the same hold registering it takes.

Under a hold on the name, a mind re-registering at boot read itself, the
delete removed it, and the registration's update changed nothing and
answered as though it had. Both now hold the mind's identity, so one waits
for the other.
"""
from __future__ import annotations

import asyncio

import asyncpg
import pytest

from comms import broker
from comms_broker_pg import (  # noqa: F401 - fixtures
    broker_store,
    fetchval,
    put_mind,
    scratch_dsn,
)

pytestmark = pytest.mark.asyncio

MIND = "5e1f0000-0000-4000-8000-0000000000b0"


async def test_a_delete_waits_while_the_minds_identity_is_held(broker_store) -> None:
    await put_mind(MIND, "contested")
    registering = await asyncpg.connect(broker_store)
    try:
        held = registering.transaction()
        await held.start()
        await registering.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"hive:mind:{MIND}")

        deleting = asyncio.create_task(broker.delete_mind("contested"))
        await asyncio.sleep(0.5)
        waited = not deleting.done()

        await held.commit()
        deleted = await asyncio.wait_for(deleting, timeout=10)
    finally:
        await registering.close()

    assert waited
    assert deleted is True
    assert await fetchval(
        "SELECT count(*) FROM core.minds WHERE mind_id = $1", MIND) == 0
