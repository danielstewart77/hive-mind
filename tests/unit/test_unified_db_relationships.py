"""The foreign keys the file split made impossible.

Each test builds a schema from SQLite stores it writes itself, so the
assertions do not move when a row is deleted from the live hive. The two that
need a database build the real one and ask Postgres what it enforces.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import uuid

import pytest

from unified_db import build_schema, schema_map


# ---------------------------------------------------------------------------
# Fixtures that stand in for the stores, never for the code under test
# ---------------------------------------------------------------------------

def _store(path, *statements: str) -> None:
    con = sqlite3.connect(path)
    for stmt in statements:
        con.execute(stmt)
    con.commit()
    con.close()


@pytest.fixture
def two_stores(tmp_path, monkeypatch):
    """Two source files whose tables land in two different schemas.

    `left.child` references `left.parent` inside its own file, and the map
    puts the two in different schemas — which is the only way to tell a build
    that resolves the target through `PLACEMENT` from one that assumes the
    reference stays in the schema it started in.
    """
    _store(
        tmp_path / "left.db",
        "CREATE TABLE parent (id TEXT PRIMARY KEY)",
        "CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT "
        "REFERENCES parent(id))",
    )
    _store(
        tmp_path / "right.db",
        "CREATE TABLE far (id TEXT PRIMARY KEY, parent_id TEXT)",
    )
    monkeypatch.setattr(schema_map, "SOURCES", {
        "left": "left.db", "right": "right.db"})
    monkeypatch.setattr(schema_map, "PLACEMENT", {
        ("left", "parent"): ("core", "parent"),
        ("left", "child"): ("board", "child"),
        ("right", "far"): ("memory", "far"),
    })
    monkeypatch.setattr(schema_map, "DROPS", {})
    monkeypatch.setattr(schema_map, "UNKEYED", {})
    monkeypatch.setattr(schema_map, "DROPPED_DEFAULTS", {})
    monkeypatch.setattr(schema_map, "CROSS_FILE", ())
    monkeypatch.setattr(schema_map, "ON_DELETE_OVERRIDE", {})
    return tmp_path


def _constraint_for(built, child_table: str) -> str:
    for line in built.ddl:
        if line.startswith("ALTER TABLE") and f'."{child_table}"' in line:
            return line
    raise AssertionError(
        f"no constraint emitted for {child_table}:\n" + "\n".join(built.ddl))


# ---------------------------------------------------------------------------
# What the generator produces
# ---------------------------------------------------------------------------

def test_a_source_declared_key_points_at_the_schema_the_map_placed_it_in(
        two_stores):
    built = build_schema.build(str(two_stores))

    clause = _constraint_for(built, "child")
    assert 'ALTER TABLE board."child"' in clause
    assert 'REFERENCES core."parent" ("id")' in clause


def test_a_relationship_the_file_split_made_impossible_is_emitted(
        two_stores, monkeypatch):
    monkeypatch.setattr(schema_map, "CROSS_FILE", (
        schema_map.Relationship(
            ("memory", "far"), ("parent_id",), ("core", "parent"), ("id",),
            "RESTRICT", "Two files could never have declared this."),
    ))

    built = build_schema.build(str(two_stores))

    clause = _constraint_for(built, "far")
    assert 'ALTER TABLE memory."far"' in clause
    assert 'REFERENCES core."parent" ("id")' in clause
    assert "ON DELETE RESTRICT" in clause


def test_a_row_pointing_nowhere_withholds_the_key_and_reports_the_count(
        two_stores):
    con = sqlite3.connect(two_stores / "left.db")
    con.execute("INSERT INTO parent (id) VALUES ('real')")
    con.executemany("INSERT INTO child (id, parent_id) VALUES (?, ?)",
                    [("a", "real"), ("b", "gone"), ("c", "also-gone")])
    con.commit()
    con.close()

    built = build_schema.build(str(two_stores))

    assert not [d for d in built.ddl if d.startswith("ALTER TABLE")]
    assert any("2 row(s)" in w and "board.child.parent_id" in w
               for w in built.withheld), built.withheld


def test_clean_rows_declare_the_same_key(two_stores):
    con = sqlite3.connect(two_stores / "left.db")
    con.execute("INSERT INTO parent (id) VALUES ('real')")
    con.execute("INSERT INTO child (id, parent_id) VALUES ('a', 'real')")
    con.commit()
    con.close()

    built = build_schema.build(str(two_stores))

    assert 'REFERENCES core."parent" ("id")' in _constraint_for(built, "child")
    assert built.withheld == []


def test_an_empty_string_counts_as_a_row_pointing_nowhere(two_stores):
    """SQLite tolerated it; Postgres treats '' as a value and refuses it."""
    con = sqlite3.connect(two_stores / "left.db")
    con.execute("INSERT INTO parent (id) VALUES ('real')")
    con.execute("INSERT INTO child (id, parent_id) VALUES ('a', '')")
    con.commit()
    con.close()

    built = build_schema.build(str(two_stores))

    assert any("1 row(s)" in w for w in built.withheld), built.withheld


def test_a_key_onto_a_dropped_table_refuses_the_build(two_stores, monkeypatch):
    monkeypatch.setattr(schema_map, "DROPS", {
        ("left", "parent"): "Decided away after the key was written."})
    monkeypatch.setattr(schema_map, "PLACEMENT", {
        ("left", "child"): ("board", "child"),
        ("right", "far"): ("memory", "far"),
    })

    with pytest.raises(build_schema.DanglingReference) as caught:
        build_schema.build(str(two_stores))

    assert "parent" in str(caught.value)


def test_a_reason_written_against_a_missing_column_refuses_the_build(
        two_stores, monkeypatch):
    monkeypatch.setattr(schema_map, "UNKEYED", {
        ("board", "child", "renamed_away"): "Looks deliberate, documents "
                                            "nothing."})

    with pytest.raises(build_schema.StaleDecision) as caught:
        build_schema.build(str(two_stores))

    assert "board.child.renamed_away" in str(caught.value)


# ---------------------------------------------------------------------------
# What Postgres then enforces
# ---------------------------------------------------------------------------
#
# The generator's output is text until a database accepts it. These two build
# the real schema from the live stores and ask Postgres, because the failures
# that matter here — a statement the apply rejected, an ON DELETE that was
# dropped on the way through, a constraint reported but never created — are
# all invisible to an assertion about a string.

CONTAINER = "hive-postgres"
DB_USER = "hive"


def _psql(database: str, sql: str, *, stdin: str | None = None) -> str:
    cmd = ["docker", "exec"]
    if stdin is not None:
        cmd.append("-i")
    cmd += [CONTAINER, "psql", "-U", DB_USER, "-d", database, "-At",
            "-v", "ON_ERROR_STOP=1"]
    if stdin is None:
        cmd += ["-c", sql]
    done = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                          timeout=180)
    if done.returncode != 0:
        raise AssertionError(f"psql failed: {done.stderr.strip()}")
    return done.stdout.strip()


@pytest.fixture(scope="module")
def built_database():
    """The real schema, applied to a throwaway database beside the live one."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available to reach Postgres")
    probe = subprocess.run(
        ["docker", "exec", CONTAINER, "pg_isready", "-U", DB_USER],
        capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"{CONTAINER} is not accepting connections")

    built = build_schema.build("/home/daniel/Storage")
    name = f"keys_test_{uuid.uuid4().hex[:10]}"
    _psql("postgres", f'CREATE DATABASE "{name}";')
    try:
        _psql(name, "", stdin="\n".join(built.ddl))
        yield name, built
    finally:
        _psql("postgres", f'DROP DATABASE IF EXISTS "{name}";')


def test_the_database_enforces_exactly_what_the_build_reported(built_database):
    name, built = built_database

    rows = _psql(name, """
        SELECT ns.nspname || '.' || cl.relname || '.' || con.conname
        FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = cl.relnamespace
        WHERE con.contype = 'f' ORDER BY 1;""")
    enforced = set(filter(None, rows.splitlines()))
    reported = {
        f"{r.child[0]}.{r.child[1]}.{r.name}" for r in built.relationships}

    assert enforced == reported


def test_the_on_delete_rule_is_the_one_the_build_decided(built_database):
    name, built = built_database
    decided = {r.name: r.on_delete for r in built.relationships}

    rows = _psql(name, """
        SELECT conname, confdeltype FROM pg_constraint
        WHERE contype = 'f' ORDER BY 1;""")
    letters = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
               "n": "SET NULL", "d": "SET DEFAULT"}
    actual = {}
    for line in filter(None, rows.splitlines()):
        conname, code = line.split("|")
        actual[conname] = letters[code]

    assert actual == decided


def test_ending_a_session_takes_its_turns_and_a_busy_mind_cannot_be_deleted(
        built_database):
    name, _ = built_database
    _psql(name, """
        INSERT INTO core.minds (mind_id, name, gateway_url, model, harness,
                                registered_at, last_seen)
            VALUES ('m-1', 'tester', 'http://example.invalid', 'a-model',
                    'claude', 0, 0);
        INSERT INTO core.sessions (id, mind_id, owner_type, owner_ref,
                                   created_at, last_active)
            VALUES ('s-1', 'm-1', 'telegram', 'someone', 0, 0);
        INSERT INTO core.session_turns (session_id, role, content)
            VALUES ('s-1', 'user', 'hello');
        INSERT INTO core.active_sessions (client_type, client_ref, session_id)
            VALUES ('telegram', 'someone', 's-1');""")

    refused = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", DB_USER, "-d", name,
         "-v", "ON_ERROR_STOP=1", "-c",
         "DELETE FROM core.minds WHERE mind_id = 'm-1';"],
        capture_output=True, text=True)
    assert refused.returncode != 0
    assert "sessions" in refused.stderr

    _psql(name, "DELETE FROM core.sessions WHERE id = 's-1';")
    assert _psql(
        name, "SELECT count(*) FROM core.session_turns "
              "WHERE session_id = 's-1';") == "0"
    assert _psql(
        name, "SELECT count(*) FROM core.active_sessions "
              "WHERE session_id = 's-1';") == "0"
    assert _psql(name, "SELECT count(*) FROM core.minds "
                       "WHERE mind_id = 'm-1';") == "1"
    _psql(name, "DELETE FROM core.minds WHERE mind_id = 'm-1';")


def test_retiring_a_proxy_credential_empties_the_models_that_named_it(
        built_database):
    name, _ = built_database
    _psql(name, """
        INSERT INTO proxy.credentials (id, name, kind, secret)
            VALUES (9001, 'test-cred', 'static', 'x');
        INSERT INTO proxy.models (id, deployment_name, target_uri,
                                  credential_id)
            VALUES (9001, 'test-model', 'https://example.invalid', 9001);
        DELETE FROM proxy.credentials WHERE id = 9001;""")

    assert _psql(name, "SELECT credential_id IS NULL FROM proxy.models "
                       "WHERE id = 9001;") == "t"
    _psql(name, "DELETE FROM proxy.models WHERE id = 9001;")
