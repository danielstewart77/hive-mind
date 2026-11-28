"""The sweep that may not drop a live run's database.

Nine runs of this suite died inside seven minutes because a concurrent run
swept the database they were using. These three are a set: a claimed database
survives a sweep, an unclaimed one still goes — a sweep that dropped nothing
at all would pass the first on its own — and a sweep hands back every claim
it took to ask, since a sweeper still holding them is a run that can never
drop its own database.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from tests._scratch import open_claimed, sweep_stale
from tests.conftest import ADMIN_DSN, NO_SERVER


#: A name no concurrent run's prefix sweep will ever match, so a test can
#: ask this sweep what it decided about one database of its own.
_UNSWEPT = "hive_data_claimcheck_"


@pytest.fixture(autouse=True)
def _needs_a_server():
    if not ADMIN_DSN:
        pytest.skip(NO_SERVER)


async def _exists(admin, name: str) -> bool:
    return bool(await admin.fetchval(
        "SELECT 1 FROM pg_database WHERE datname = $1", name))


async def test_a_sweep_leaves_a_database_another_run_has_claimed():
    """The claim, not the backend count. A run's own database has no
    connections to it between tests, so counting backends answered "nobody"
    about a database in active use and the owning run lost every remaining
    test to `database "..." does not exist`."""
    scratch = await open_claimed(ADMIN_DSN, "hive_data_other_")
    sweeper = await asyncpg.connect(ADMIN_DSN)
    try:
        # Nothing is connected to it, which is the state the old guard read
        # as idle.
        assert await sweeper.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = $1",
            scratch.name) == 0

        dropped = await sweep_stale(sweeper)

        assert scratch.name not in dropped
        assert await _exists(sweeper, scratch.name)
    finally:
        await sweeper.close()
        await scratch.close()


async def test_a_sweep_drops_a_leftover_nobody_has_claimed():
    """What the sweep is for: the database a killed run left behind, which
    otherwise accumulates one per interrupted run for ever.

    Named to the sweep rather than left to the prefix match: a second run of
    this suite sweeps the same server, and a leftover another sweeper is
    halfway through dropping would make this assertion about its timing
    rather than about this sweep's decision."""
    leftover = f"{_UNSWEPT}{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{leftover}"')
    finally:
        await admin.close()

    sweeper = await asyncpg.connect(ADMIN_DSN)
    try:
        dropped = await sweep_stale(sweeper, only=[leftover])

        assert dropped == [leftover]
        assert not await _exists(sweeper, leftover)
    finally:
        await sweeper.execute(f'DROP DATABASE IF EXISTS "{leftover}"')
        await sweeper.close()


async def test_a_claim_survives_the_connection_that_asked_about_it():
    """A sweep takes the claim to test it and must hand it straight back:
    otherwise the first sweep of a run leaves every name it looked at locked
    by its own connection, and the run that owns one can never drop it."""
    leftover = f"{_UNSWEPT}{uuid.uuid4().hex[:12]}"
    sweeper = await asyncpg.connect(ADMIN_DSN)
    try:
        await sweeper.execute(f'CREATE DATABASE "{leftover}"')
        await sweep_stale(sweeper, only=[leftover])

        assert await sweeper.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid()") == 0
    finally:
        await sweeper.execute(f'DROP DATABASE IF EXISTS "{leftover}"')
        await sweeper.close()
