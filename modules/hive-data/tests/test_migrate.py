"""`hive_data.migrate`: what an installer runs against a database.

Every test takes a database of its own: each one starts from a different
state of the world — empty, holding only `core`, a hive applied before the
record existed — and the suite's shared scratch database is already a
finished one.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from hive_data import migrate
from hive_data.errors import MigrationFailed, MigrationRefused
from hive_data.schema import COLUMNS, bare, schema_of

from tests._scratch import claimed_database

#: Every step after the four baseline files. A hive the baseline reached
#: before the record existed is adopted for those four and still runs these.
LATER = [s.version for s in migrate.steps() if s.version > 4]
from tests.conftest import (
    ADMIN_DSN, BOARD_SCHEMA_SQL, MEMORY_SCHEMA_SQL, NO_SERVER, SCHEMA_SQL,
    TOOLS_SCHEMA_SQL)

pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason=NO_SERVER)


async def _missing_tables(dsn: str) -> list[str]:
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch(
            "SELECT n.nspname || '.' || c.relname AS t FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r'")
    finally:
        await connection.close()
    have = {r["t"] for r in rows}
    return sorted(f"{schema_of(t)}.{bare(t)}" for t in COLUMNS
                  if f"{schema_of(t)}.{bare(t)}" not in have)


async def test_an_empty_database_gets_every_declared_table_and_a_second_apply_keeps_every_row():
    async with claimed_database(ADMIN_DSN, "hive_data_other_") as dsn:
        first = await migrate.apply(dsn)
        assert await _missing_tables(dsn) == []
        assert {schema_of(t) for t in COLUMNS} == {
            "core", "memory", "tools", "board", "staff"}
        assert first["applied"] == [s.version for s in migrate.steps()]

        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(
                "INSERT INTO core.conversations (id, created_at) "
                "VALUES ('kept', 1)")
            again = await migrate.apply(dsn)
            assert again["applied"] == [] and again["adopted"] == []
            assert await connection.fetchval(
                "SELECT count(*) FROM core.conversations "
                "WHERE id = 'kept'") == 1
        finally:
            await connection.close()


async def test_a_hive_applied_before_the_record_is_adopted_without_running_a_step():
    async with claimed_database(ADMIN_DSN, "hive_data_other_") as dsn:
        connection = await asyncpg.connect(dsn)
        try:
            for path in (SCHEMA_SQL, MEMORY_SCHEMA_SQL, TOOLS_SCHEMA_SQL,
                         BOARD_SCHEMA_SQL):
                await connection.execute(path.read_text())
            await connection.execute(
                "INSERT INTO core.conversations (id, created_at) "
                "VALUES ('kept', 1)")
            out = await migrate.apply(dsn)
            assert out["applied"] == LATER
            assert out["adopted"] == [1, 2, 3, 4]
            assert await connection.fetchval(
                "SELECT count(*) FROM core.conversations") == 1
        finally:
            await connection.close()


async def test_a_database_holding_only_core_is_completed():
    async with claimed_database(ADMIN_DSN, "hive_data_other_") as dsn:
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(SCHEMA_SQL.read_text())
        finally:
            await connection.close()
        out = await migrate.apply(dsn)
        assert out["adopted"] == [1]
        assert out["applied"] == [2, 3, 4, *LATER]
        assert await _missing_tables(dsn) == []


async def test_a_newer_step_is_applied_to_a_current_hive_and_recorded():
    async with claimed_database(ADMIN_DSN, "hive_data_other_") as dsn:
        await migrate.apply(dsn)
        newer = migrate.Step(
            99, "probe", "CREATE TABLE core.probe_step (x integer);")
        out = await migrate.apply(dsn, chosen=migrate.steps() + [newer])
        assert out["applied"] == [99] and out["version"] == 99
        connection = await asyncpg.connect(dsn)
        try:
            assert await connection.fetchval(
                "SELECT to_regclass('core.probe_step') IS NOT NULL")
        finally:
            await connection.close()
        report = await migrate.check(dsn, chosen=migrate.steps() + [newer])
        assert report["ok"] and report["pending"] == []


async def test_a_failing_step_leaves_nothing_of_itself_and_the_earlier_steps_recorded():
    async with claimed_database(ADMIN_DSN, "hive_data_other_") as dsn:
        broken = migrate.Step(
            99, "broken",
            "CREATE TABLE core.half_done (x integer); SELECT 1/0;")
        with pytest.raises(MigrationFailed) as failed:
            await migrate.apply(dsn, chosen=migrate.steps() + [broken])
        assert failed.value.version == 99
        assert failed.value.applied == [1, 2, 3, 4, *LATER]
        connection = await asyncpg.connect(dsn)
        try:
            assert not await connection.fetchval(
                "SELECT to_regclass('core.half_done') IS NOT NULL")
        finally:
            await connection.close()
        report = await migrate.check(dsn, chosen=migrate.steps() + [broken])
        assert report["recorded"] == [1, 2, 3, 4, *LATER]
        assert report["pending"] == [{"version": 99, "name": "broken"}]


def _as(dsn: str, user: str, password: str) -> str:
    parts = urlsplit(dsn)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}"))


async def test_a_role_that_may_not_add_vector_is_refused_before_anything_is_written():
    role = f"hive_data_test_role_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    server = await asyncpg.connect(ADMIN_DSN)
    await server.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    try:
        async with claimed_database(ADMIN_DSN, "hive_data_other_") as dsn:
            admin = await asyncpg.connect(dsn)
            try:
                assert not await admin.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension "
                    "WHERE extname = 'vector')")
                # Everything an ordinary owner would hold, so the one thing
                # missing is the superuser `vector` demands.
                name = await admin.fetchval("SELECT current_database()")
                await admin.execute(
                    f'GRANT CREATE ON DATABASE "{name}" TO {role}')
                await admin.execute(
                    f"GRANT CREATE ON SCHEMA public TO {role}")
                as_role = _as(dsn, role, password)

                report = await migrate.check(as_role)
                assert not report["ok"]
                assert any("vector" in p for p in report["problems"])
                with pytest.raises(MigrationRefused) as refused:
                    await migrate.apply(as_role)
                assert any("vector" in p for p in refused.value.problems)

                assert await admin.fetchval(
                    "SELECT count(*) FROM pg_namespace WHERE nspname = "
                    "ANY($1)",
                    ["core", "memory", "tools", "board",
                     migrate.RECORD_SCHEMA]) == 0
                await admin.execute(
                    f"REVOKE ALL ON SCHEMA public FROM {role}")
            finally:
                await admin.close()
    finally:
        try:
            await server.execute(f"DROP ROLE IF EXISTS {role}")
        finally:
            await server.close()


async def test_a_later_step_needing_create_or_references_is_refused_up_front():
    """A step after the baseline that makes a schema needs CREATE on the
    database, and one with a key into an existing table needs REFERENCES on
    it. Both are reported by `check` rather than failing halfway through."""
    role = f"hive_data_test_role_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    newer = migrate.Step(
        99, "keyed",
        "CREATE SCHEMA keyed; CREATE TABLE keyed.t "
        "(s text REFERENCES core.sessions(id));")
    server = await asyncpg.connect(ADMIN_DSN)
    await server.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    try:
        async with claimed_database(ADMIN_DSN, "hive_data_other_") as dsn:
            await migrate.apply(dsn)
            admin = await asyncpg.connect(dsn)
            try:
                # Enough to read the record, and nothing else.
                await admin.execute(
                    f"GRANT USAGE ON SCHEMA {migrate.RECORD_SCHEMA} TO {role}; "
                    f"GRANT SELECT ON {migrate.RECORD} TO {role}")
            finally:
                await admin.close()
            as_role = _as(dsn, role, password)

            report = await migrate.check(as_role,
                                         chosen=migrate.steps() + [newer])

            assert not report["ok"]
            assert any("no CREATE on database" in p
                       for p in report["problems"])
            assert any("REFERENCES on core.sessions" in p
                       for p in report["problems"])
    finally:
        try:
            await server.execute(f"DROP ROLE IF EXISTS {role}")
        finally:
            await server.close()
