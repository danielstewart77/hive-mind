"""`hive_data.migrate`: what an installer runs against a database.

Every test takes a database of its own: each one starts from a different
state of the world — empty, holding only `core`, a hive applied before the
record existed — and the suite's shared scratch database is already a
finished one.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from hive_data import migrate, schema
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


async def test_a_step_creating_a_role_is_refused_up_front_without_createrole():
    """A step that makes a role needs CREATEROLE unless the role is there."""
    role = f"hive_data_test_role_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    absent = f"hive_data_test_absent_{uuid.uuid4().hex[:8]}"
    present = migrate.Step(98, "has_role", "CREATE ROLE hive_staff NOLOGIN;")
    newer = migrate.Step(99, "makes_role", f"CREATE ROLE {absent} NOLOGIN;")
    creator = f"hive_data_test_creator_{uuid.uuid4().hex[:8]}"
    server = await asyncpg.connect(ADMIN_DSN)
    await server.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    await server.execute(
        f"CREATE ROLE {creator} LOGIN CREATEROLE PASSWORD '{password}'")
    try:
        async with claimed_database(ADMIN_DSN, "hive_data_other_") as dsn:
            await migrate.apply(dsn)
            admin = await asyncpg.connect(dsn)
            try:
                await admin.execute(
                    f"GRANT USAGE ON SCHEMA {migrate.RECORD_SCHEMA} "
                    f"TO {role}, {creator}; "
                    f"GRANT SELECT ON {migrate.RECORD} TO {role}, {creator}")
            finally:
                await admin.close()
            chosen = migrate.steps() + [present, newer]

            report = await migrate.check(_as(dsn, role, password),
                                         chosen=chosen)
            allowed = await migrate.check(_as(dsn, creator, password),
                                          chosen=chosen)

            assert not report["ok"]
            assert any(f"creates role {absent}" in p
                       for p in report["problems"])
            assert not any("creates role hive_staff" in p
                           for p in report["problems"])
            assert not any("creates role" in p
                           for p in allowed["problems"])
    finally:
        try:
            await server.execute(f"DROP ROLE IF EXISTS {role}")
            await server.execute(f"DROP ROLE IF EXISTS {creator}")
        finally:
            await server.close()


#: The columns the previous release declared, as that release shipped them.
#: Refreshed with the release procedure when a release is tagged (see the
#: README, "Versions"): after that, the steps added since are what this
#: suite proves the release just gone can still live beside.
PREVIOUS = json.loads(
    (Path(__file__).parent / "previous_release_columns.json").read_text())


@pytest.mark.parametrize("version", [s.version for s in migrate.steps()
                                     if s.version > 4])
def test_a_shipped_step_only_adds(version):
    step = next(s for s in migrate.steps() if s.version == version)
    assert migrate.rule_violations(step) == []


@pytest.mark.parametrize("sql, mentions", [
    ("ALTER TABLE core.minds ADD COLUMN x integer NOT NULL;", "NOT NULL"),
    ("ALTER TABLE core.minds ALTER COLUMN owner SET NOT NULL;", "owner"),
    ("ALTER TABLE core.minds DROP COLUMN voice;", "drops a column"),
    ("ALTER TABLE core.minds RENAME COLUMN voice TO sound;", "renames"),
    ("ALTER TABLE core.minds ALTER COLUMN voice TYPE integer;", "retypes"),
    ("DROP TABLE core.minds;", "drops a table"),
    ("DROP SCHEMA core CASCADE;", "drops a schema"),
    ("DROP TYPE core.kind;", "drops a type"),
    ("TRUNCATE core.minds;", "truncates"),
    ("ALTER TABLE core.minds RENAME TO people;", "renames"),
    ("ALTER TABLE core.minds ALTER COLUMN voice SET DATA TYPE integer;",
     "retypes"),
    ("ALTER TABLE core.minds DROP voice;", "drops a column"),
    ("INSERT INTO core.t VALUES ('--x'); DROP TABLE core.minds;",
     "drops a table"),
    ("/* ; */ DROP TABLE core.minds;", "drops a table"),
    ("INSERT INTO core.t VALUES (E'it\\'s'); DROP TABLE core.minds;",
     "drops a table"),
    ('ALTER TABLE "My T" DROP COLUMN "a b";', "drops a column"),
    ("ALTER TABLE core.minds ADD COLUMN a text NOT NULL "
     "CHECK (a <> 'DEFAULT');", "NOT NULL"),
    ("ALTER TABLE core.a ALTER COLUMN x SET DEFAULT 1; "
     "ALTER TABLE core.b ALTER COLUMN x SET NOT NULL;", "NOT NULL"),
])
def test_a_step_that_breaks_the_previous_release_is_named(sql, mentions):
    found = migrate.rule_violations(migrate.Step(99, "bad", sql))
    assert found and any(mentions in line for line in found)


@pytest.mark.parametrize("sql", [
    "ALTER TABLE core.minds ADD COLUMN x integer;",
    "ALTER TABLE core.minds ADD COLUMN x integer NOT NULL DEFAULT 0;",
    "ALTER TABLE core.minds ALTER COLUMN owner SET DEFAULT '', "
    "ALTER COLUMN owner SET NOT NULL;",
    "CREATE TABLE core.fresh (x integer NOT NULL);",
    "ALTER TABLE core.minds ADD COLUMN n bigint NOT NULL "
    "GENERATED ALWAYS AS IDENTITY;",
    "ALTER TABLE core.minds ADD CONSTRAINT u UNIQUE (voice);",
    "ALTER TABLE core.minds ADD COLUMN c integer CHECK (c > 0) "
    "REFERENCES core.other (id);",
    "INSERT INTO core.t VALUES ('a;b -- c DROP TABLE x');",
    "/* a /* b */ ; DROP TABLE core.minds; */ SELECT 1;",
    "CREATE FUNCTION core.f() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN DROP TABLE x; RETURN NEW; END $$;",
])
def test_a_step_that_only_adds_is_not_named(sql):
    assert migrate.rule_violations(migrate.Step(99, "ok", sql)) == []


async def test_the_previous_release_still_verifies_and_writes_against_the_current_steps(
        monkeypatch):
    async with claimed_database(ADMIN_DSN, "hive_data_other_") as dsn:
        await migrate.apply(dsn)
        # The previous release, as it declares its columns.
        monkeypatch.setattr(
            schema, "COLUMNS", {t: tuple(c) for t, c in PREVIOUS.items()})
        connection = await asyncpg.connect(dsn)
        try:
            await schema.verify(connection)

            # Every column a later step added to a table the old release
            # writes must be one an insert can leave out. (A table the old
            # release has never heard of is nothing it writes to.)
            theirs = {(schema.schema_of(t), schema.bare(t)): set(c)
                      for t, c in PREVIOUS.items()}
            rows = await connection.fetch(
                "SELECT table_schema, table_name, column_name "
                "FROM information_schema.columns "
                "WHERE is_nullable = 'NO' AND column_default IS NULL "
                "AND is_identity = 'NO' AND is_generated = 'NEVER'")
            unknown = sorted(
                f"{r['table_schema']}.{r['table_name']}.{r['column_name']}"
                for r in rows
                if (r["table_schema"], r["table_name"]) in theirs
                and r["column_name"]
                not in theirs[(r["table_schema"], r["table_name"])])
            assert unknown == []

            # And an old-release write: an insert naming only the columns the
            # previous release knows, into a table later steps changed.
            await connection.execute(
                "INSERT INTO board.work_items "
                "(id, kind, title, \"position\") VALUES "
                "('old-release-row', 'task', 'written by the previous "
                "release', 1)")
            assert await connection.fetchval(
                "SELECT count(*) FROM board.work_items "
                "WHERE id = 'old-release-row'") == 1
        finally:
            await connection.close()
