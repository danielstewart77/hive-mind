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


#: What the one database is supposed to relate, written out rather than read
#: back off the build. A reconciliation between `Build.ddl` and
#: `Build.relationships` would pass for any wrongness applied consistently to
#: both — a key silently dropped, every constraint renamed — because the two
#: sides move together. This list is the independent side.
EXPECTED: set[tuple[str, str, str, str]] = {
    ("board.design_sessions", "conversation_id", "core.sessions", "SET NULL"),
    ("board.design_sessions", "mind_id", "core.minds", "RESTRICT"),
    ("board.work_items", "parent_id", "board.work_items", "NO ACTION"),
    ("core.active_sessions", "session_id", "core.sessions", "CASCADE"),
    ("core.context_overhead", "mind_id", "core.minds", "CASCADE"),
    ("core.group_members", "group_name", "core.groups", "NO ACTION"),
    ("core.group_members", "user_id", "core.users", "NO ACTION"),
    ("core.group_sessions", "moderator_mind_id", "core.minds", "RESTRICT"),
    ("core.messages", "conversation_id", "core.conversations", "CASCADE"),
    ("core.session_turns", "session_id", "core.sessions", "CASCADE"),
    ("core.sessions", "group_session_id", "core.group_sessions", "SET NULL"),
    ("core.sessions", "mind_id", "core.minds", "RESTRICT"),
    ("core.surface_claims", "mind_id", "core.minds", "CASCADE"),
    ("memory.edges", "mind_id", "core.minds", "RESTRICT"),
    ("memory.edges", "source_id", "memory.nodes", "CASCADE"),
    ("memory.edges", "target_id", "memory.nodes", "CASCADE"),
    ("memory.nodes", "type", "memory.schema_types", "RESTRICT"),
    ("memory.schema_edges", "source_type", "memory.schema_types", "RESTRICT"),
    ("memory.schema_edges", "target_type", "memory.schema_types", "RESTRICT"),
    ("memory.soul_changes", "node_id", "memory.nodes", "RESTRICT"),
    ("proxy.keys", "client_id", "proxy.clients", "CASCADE"),
    ("proxy.models", "credential_id", "proxy.credentials", "SET NULL"),
    ("proxy.models", "provider_id", "proxy.providers", "SET NULL"),
    ("proxy.providers", "credential_id", "proxy.credentials", "SET NULL"),
    ("proxy.usage_log", "client_id", "proxy.clients", "NO ACTION"),
    ("proxy.usage_log", "key_id", "proxy.keys", "NO ACTION"),
}

_DELETE_RULE = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
                "n": "SET NULL", "d": "SET DEFAULT"}


def test_the_database_relates_exactly_what_the_hive_relates(built_database):
    name, _ = built_database

    rows = _psql(name, """
        SELECT cn.nspname || '.' || cl.relname,
               string_agg(ca.attname, '/' ORDER BY k.ord),
               pn.nspname || '.' || pa.relname,
               con.confdeltype
        FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        JOIN pg_namespace cn ON cn.oid = cl.relnamespace
        JOIN pg_class pa ON pa.oid = con.confrelid
        JOIN pg_namespace pn ON pn.oid = pa.relnamespace
        JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(att, ord)
             ON TRUE
        JOIN pg_attribute ca ON ca.attrelid = cl.oid AND ca.attnum = k.att
        WHERE con.contype = 'f'
        GROUP BY 1, 3, 4, con.oid ORDER BY 1, 2;""")

    enforced = set()
    for line in filter(None, rows.splitlines()):
        child, cols, parent, code = line.split("|")
        enforced.add((child, cols, parent, _DELETE_RULE[code]))

    assert enforced == EXPECTED


def test_a_table_that_references_itself_defers_to_commit(built_database):
    """Without this the loader cannot insert such a table in one pass: its
    order is per row, not per table."""
    name, _ = built_database

    rows = _psql(name, """
        SELECT conname, condeferrable FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        WHERE con.contype = 'f' AND con.conrelid = con.confrelid
        ORDER BY 1;""")
    deferred = dict(line.split("|") for line in filter(None, rows.splitlines()))

    assert deferred == {"work_items_parent_id_fk_work_items": "t"}


def test_a_statement_that_fails_leaves_no_half_applied_schema(built_database):
    """The whole DDL is one transaction, so a bad apply is a no-op."""
    _, built = built_database
    name = f"keys_atomic_{uuid.uuid4().hex[:10]}"
    _psql("postgres", f'CREATE DATABASE "{name}";')
    try:
        broken = list(built.ddl)
        broken.insert(-1, "CREATE TABLE core.\"boom\" (x no_such_type);")
        with pytest.raises(AssertionError):
            _psql(name, "", stdin="\n".join(broken))

        assert _psql(name, "SELECT count(*) FROM information_schema.tables "
                           "WHERE table_schema = 'core';") == "0"
    finally:
        _psql("postgres", f'DROP DATABASE IF EXISTS "{name}";')


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


# ---------------------------------------------------------------------------
# The judgement inside the check, driven by fixtures rather than live rows
# ---------------------------------------------------------------------------

def test_a_null_reference_is_not_a_row_pointing_nowhere(two_stores):
    """A foreign key permits NULL. Counting it as an offender would withhold
    every optional relationship in the hive."""
    con = sqlite3.connect(two_stores / "left.db")
    con.execute("INSERT INTO parent (id) VALUES ('real')")
    con.execute("INSERT INTO child (id, parent_id) VALUES ('a', NULL)")
    con.commit()
    con.close()

    built = build_schema.build(str(two_stores))

    assert built.withheld == []
    assert 'REFERENCES core."parent" ("id")' in _constraint_for(built, "child")


def test_a_wildcard_in_the_data_does_not_count_as_a_match(two_stores):
    """The offender count is an equality, not a pattern match."""
    con = sqlite3.connect(two_stores / "left.db")
    con.execute("INSERT INTO parent (id) VALUES ('real')")
    con.execute("INSERT INTO child (id, parent_id) VALUES ('a', '%')")
    con.commit()
    con.close()

    built = build_schema.build(str(two_stores))

    assert any("1 row(s)" in w for w in built.withheld), built.withheld


def test_a_relationship_the_data_refuses_moves_the_exit_code(two_stores):
    """A recorded decision never goes away and must not move it; a row
    pointing nowhere must, or the documented pipe into psql reports success
    over a schema that is quietly missing a constraint."""
    con = sqlite3.connect(two_stores / "left.db")
    con.execute("INSERT INTO child (id, parent_id) VALUES ('a', 'gone')")
    con.commit()
    con.close()

    refused = build_schema.build(str(two_stores))
    assert refused.withheld and refused.notes == []

    con = sqlite3.connect(two_stores / "left.db")
    con.execute("DELETE FROM child")
    con.commit()
    con.close()
    clean = build_schema.build(str(two_stores))

    assert clean.withheld == []


def test_a_column_that_is_not_a_reference_is_reported_with_its_reason(
        two_stores, monkeypatch):
    monkeypatch.setattr(schema_map, "UNKEYED", {
        ("board", "child", "parent_id"): "A sentinel, not a reference."})

    built = build_schema.build(str(two_stores))

    assert any("board.child.parent_id" in u and "sentinel" in u
               for u in built.unkeyed), built.unkeyed
    assert built.withheld == []


def test_a_decision_is_not_a_note(two_stores, monkeypatch):
    """`notes` drives the exit code, so a decision that never goes away must
    not land there."""
    monkeypatch.setattr(schema_map, "DROPPED_DEFAULTS", {
        ("board", "child", "parent_id"): "Points at nothing that can exist."})
    built = build_schema.build(str(two_stores))

    assert built.notes == []
    assert any("parent_id" in d and "dropped" in d for d in built.decisions)


def test_a_relationship_that_cannot_be_counted_refuses_the_build(
        two_stores, monkeypatch):
    """A mistyped column must not read as a relationship nobody could check."""
    monkeypatch.setattr(schema_map, "CROSS_FILE", (
        schema_map.Relationship(
            ("memory", "far"), ("no_such_column",), ("core", "parent"),
            ("id",), "RESTRICT", "Typed wrong."),
    ))

    with pytest.raises(build_schema.Uncountable) as caught:
        build_schema.build(str(two_stores))

    assert "no_such_column" in str(caught.value)


def test_both_columns_of_a_two_column_key_are_checked(tmp_path, monkeypatch):
    _store(
        tmp_path / "only.db",
        "CREATE TABLE parent (a TEXT, b TEXT, PRIMARY KEY (a, b))",
        "CREATE TABLE child (id TEXT PRIMARY KEY, a TEXT, b TEXT, "
        "FOREIGN KEY (a, b) REFERENCES parent(a, b))",
        "INSERT INTO parent (a, b) VALUES ('x', 'y')",
        "INSERT INTO child (id, a, b) VALUES ('1', 'x', 'WRONG')",
    )
    monkeypatch.setattr(schema_map, "SOURCES", {"only": "only.db"})
    monkeypatch.setattr(schema_map, "PLACEMENT", {
        ("only", "parent"): ("core", "parent"),
        ("only", "child"): ("board", "child")})
    for name in ("DROPS", "UNKEYED", "DROPPED_DEFAULTS", "ON_DELETE_OVERRIDE"):
        monkeypatch.setattr(schema_map, name, {})
    monkeypatch.setattr(schema_map, "CROSS_FILE", ())

    built = build_schema.build(str(tmp_path))

    assert any("1 row(s)" in w for w in built.withheld), built.withheld


def test_a_key_naming_no_target_column_resolves_the_parents_primary_key(
        tmp_path, monkeypatch):
    _store(
        tmp_path / "only.db",
        "CREATE TABLE parent (id TEXT PRIMARY KEY)",
        "CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT "
        "REFERENCES parent)",
    )
    monkeypatch.setattr(schema_map, "SOURCES", {"only": "only.db"})
    monkeypatch.setattr(schema_map, "PLACEMENT", {
        ("only", "parent"): ("core", "parent"),
        ("only", "child"): ("board", "child")})
    for name in ("DROPS", "UNKEYED", "DROPPED_DEFAULTS", "ON_DELETE_OVERRIDE"):
        monkeypatch.setattr(schema_map, name, {})
    monkeypatch.setattr(schema_map, "CROSS_FILE", ())

    built = build_schema.build(str(tmp_path))

    assert 'REFERENCES core."parent" ("id")' in _constraint_for(built, "child")


def test_the_same_relationship_declared_twice_is_emitted_once(
        two_stores, monkeypatch):
    monkeypatch.setattr(schema_map, "CROSS_FILE", (
        schema_map.Relationship(
            ("board", "child"), ("parent_id",), ("core", "parent"), ("id",),
            "NO ACTION", "Also written out by hand."),
    ))

    built = build_schema.build(str(two_stores))

    assert len([d for d in built.ddl if d.startswith("ALTER TABLE")]) == 1


def test_a_key_whose_sides_disagree_in_width_refuses_the_build(
        two_stores, monkeypatch):
    """Postgres refuses it at apply time, inside the one transaction, taking
    the whole schema with it."""
    monkeypatch.setattr(schema_map, "CROSS_FILE", (
        schema_map.Relationship(
            ("memory", "far"), ("id", "parent_id"), ("core", "parent"),
            ("id",), "RESTRICT", "Two columns against one."),
    ))

    with pytest.raises(build_schema.DanglingReference) as caught:
        build_schema.build(str(two_stores))

    assert "2 column(s) against 1" in str(caught.value)


def test_a_key_onto_a_column_that_is_not_unique_refuses_the_build(
        tmp_path, monkeypatch):
    _store(
        tmp_path / "only.db",
        "CREATE TABLE parent (id TEXT PRIMARY KEY, label TEXT)",
        "CREATE TABLE child (id TEXT PRIMARY KEY, label TEXT)",
    )
    monkeypatch.setattr(schema_map, "SOURCES", {"only": "only.db"})
    monkeypatch.setattr(schema_map, "PLACEMENT", {
        ("only", "parent"): ("core", "parent"),
        ("only", "child"): ("board", "child")})
    for name in ("DROPS", "UNKEYED", "DROPPED_DEFAULTS", "ON_DELETE_OVERRIDE"):
        monkeypatch.setattr(schema_map, name, {})
    monkeypatch.setattr(schema_map, "CROSS_FILE", (
        schema_map.Relationship(
            ("board", "child"), ("label",), ("core", "parent"), ("label",),
            "RESTRICT", "Onto a column nothing makes unique."),
    ))

    with pytest.raises(build_schema.DanglingReference) as caught:
        build_schema.build(str(tmp_path))

    assert "no primary key or unique constraint" in str(caught.value)


def test_a_key_onto_an_inline_unique_rather_than_the_primary_key_is_allowed():
    """Six of the hive's keys reference `minds.mind_id`, whose uniqueness is
    an inline UNIQUE while the primary key is `name`."""
    built = build_schema.build("/home/daniel/Storage")

    assert any(r.parent == ("core", "minds") and r.parent_columns == ("mind_id",)
               for r in built.relationships)


def test_a_merge_losers_rows_are_counted_too(tmp_path, monkeypatch):
    """The winner decides the shape; it does not decide whose rows arrive."""
    _store(
        tmp_path / "win.db",
        "CREATE TABLE parent (id TEXT PRIMARY KEY)",
        "CREATE TABLE member (id TEXT PRIMARY KEY, parent_id TEXT)",
        "INSERT INTO parent (id) VALUES ('real')",
        "INSERT INTO member (id, parent_id) VALUES ('a', 'real')",
    )
    _store(
        tmp_path / "lose.db",
        "CREATE TABLE member (id TEXT PRIMARY KEY, parent_id TEXT)",
        "INSERT INTO member (id, parent_id) VALUES ('b', 'nowhere')",
    )
    monkeypatch.setattr(schema_map, "SOURCES", {
        "win": "win.db", "lose": "lose.db"})
    monkeypatch.setattr(schema_map, "PLACEMENT", {
        ("win", "parent"): ("core", "parent"),
        ("win", "member"): ("core", "member")})
    monkeypatch.setattr(schema_map, "MERGES", {
        ("lose", "member"): ("core", "member")})
    monkeypatch.setattr(schema_map, "MERGE_WINNER", {
        ("core", "member"): ("win", "member")})
    for name in ("DROPS", "UNKEYED", "DROPPED_DEFAULTS", "ON_DELETE_OVERRIDE"):
        monkeypatch.setattr(schema_map, name, {})
    monkeypatch.setattr(schema_map, "CROSS_FILE", (
        schema_map.Relationship(
            ("core", "member"), ("parent_id",), ("core", "parent"), ("id",),
            "RESTRICT", "Clean in the winner, broken in the loser."),
    ))

    built = build_schema.build(str(tmp_path))

    assert any("1 row(s)" in w for w in built.withheld), built.withheld
