"""The scratch databases a suite creates, and the claim on each one.

Shipped inside the package rather than living in this repository's own
`tests/`, because it is not only this repository's suite that needs it: a
service moving onto the one database needs a per-run database of its own with
these schemas applied, and the hive now has several of them. A second copy of
this machinery, with its own prefixes and its own sweep, is exactly how the
incident below happens again — one suite's sweep dropping another suite's
database out from under it. One implementation, one allow-list.

Three reviewers each running pytest against one server is the normal case
during a grill, and every run sweeps the databases that killed runs left
behind. The sweep used to decide a leftover was idle by counting backends in
`pg_stat_activity`. A live run's own database has no backends for most of its
life — the pool fixture opens one per test and closes it again — so the
question was answered "nobody" about a database in active use, the sweeping
run dropped it, and the run that owned it lost every remaining test to
`database "..." does not exist`. Nine runs died that way inside seven
minutes.

So a run *claims* its database instead: a session-level advisory lock on a
connection held open for exactly as long as the database is wanted, keyed by
the database's own name. The key is taken before the `CREATE`, so there is no
unclaimed moment to lose it in. A sweep drops only what it can take the claim
for, which is exactly the set nobody is using.

Advisory locks are scoped to the database the locking session is connected
to, so every claim and every sweep is made on a connection to the *admin*
database named in `HIVE_DATA_TEST_ADMIN_DSN` — never to a scratch database,
whose own claim nobody else would ever see.
"""

from __future__ import annotations

import contextlib
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg

#: Every name this suite ever creates. Nothing else on the server uses them,
#: which is what makes sweeping them somebody's business at all.
#:
#: `lucent_test_` is the nervous system's. Its suite drives real routes
#: against a database of its own, for the same reason this one does: the live
#: `hive` rows are what a cutover is verified against, and writing test
#: credentials and test Mind nodes into them poisons that comparison.
PREFIXES = ("hive_data_test_", "hive_data_other_", "hive_data_seq_",
            "lucent_test_")

_LIKE = [prefix.replace("_", r"\_") + "%" for prefix in PREFIXES]


def with_database(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{name}"))


async def _claim_key(connection, name: str) -> int:
    """The lock key for a database name, computed by the server.

    Server-side so that a claim and a sweep agree on it without either side
    carrying a copy of the other's hash.
    """
    return await connection.fetchval("SELECT hashtextextended($1, 0)", name)


async def sweep_stale(admin, only: list[str] | None = None) -> list[str]:
    """Drop every leftover no live run holds a claim on; name what went.

    `admin` is a connection to the admin database. One leftover that refuses
    to go is skipped rather than raised: a zombie backend on somebody's
    abandoned database must not stop the run in hand from starting.

    `only` narrows the candidates to the names given, whatever they are
    called. A run sweeps the prefixes; a caller that wants this sweep's own
    decision about one database, and not a concurrent sweeper's, names it.
    """
    dropped: list[str] = []
    rows = await admin.fetch(
        "SELECT datname FROM pg_database WHERE datname = ANY($1::text[])"
        if only is not None else
        "SELECT datname FROM pg_database WHERE datname LIKE ANY($1::text[])",
        only if only is not None else _LIKE)
    for row in rows:
        name = row["datname"]
        key = await _claim_key(admin, name)
        if not await admin.fetchval("SELECT pg_try_advisory_lock($1)", key):
            continue
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()", name)
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
            dropped.append(name)
        except asyncpg.PostgresError:
            continue
        finally:
            await admin.fetchval("SELECT pg_advisory_unlock($1)", key)
    return dropped


class ClaimedDatabase:
    """A scratch database and the one connection holding its claim."""

    def __init__(self, keeper, name: str, dsn: str) -> None:
        self._keeper = keeper
        self.name = name
        self.dsn = dsn

    async def close(self) -> None:
        """Drop the database and release the claim, in that order."""
        try:
            await self._keeper.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()", self.name)
            await self._keeper.execute(
                f'DROP DATABASE IF EXISTS "{self.name}"')
        finally:
            await self._keeper.close()


async def open_claimed(admin_dsn: str, prefix: str) -> ClaimedDatabase:
    """Create a scratch database with its claim already held.

    The key is taken before the `CREATE`, so there is no moment where the
    database exists unclaimed and a concurrent sweep could take it.
    """
    if prefix not in PREFIXES:
        raise ValueError(
            f"{prefix!r} is not a name this suite sweeps: {PREFIXES}")
    name = f"{prefix}{uuid.uuid4().hex[:12]}"
    keeper = await asyncpg.connect(admin_dsn)
    try:
        key = await _claim_key(keeper, name)
        await keeper.fetchval("SELECT pg_advisory_lock($1)", key)
        await keeper.execute(f'CREATE DATABASE "{name}"')
    except BaseException:
        await keeper.close()
        raise
    return ClaimedDatabase(keeper, name, with_database(admin_dsn, name))


@contextlib.asynccontextmanager
async def claimed_database(admin_dsn: str, prefix: str):
    """A scratch database of this suite's own, claimed while it is wanted.

    Yields its DSN. The claim is released and the database dropped on the way
    out, in that order, by closing the one connection that held both.
    """
    scratch = await open_claimed(admin_dsn, prefix)
    try:
        yield scratch.dsn
    finally:
        await scratch.close()
