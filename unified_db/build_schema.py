"""Emit the one database's DDL from the SQLite stores, per `schema_map`.

Read through `PRAGMA table_info` and `PRAGMA index_list` rather than by
parsing `CREATE TABLE` text. The sources carry comments inside their DDL,
partial indexes, expression indexes and quoted identifiers, and a regex over
that is how a column quietly changes type in a migration that reports
success.

Foreign keys come from two places. The ones a single source file could
declare are read from it (`PRAGMA foreign_key_list`) and remapped through
`schema_map.PLACEMENT`; the ones the file split made impossible are declared
in `schema_map.CROSS_FILE`. Neither is emitted on faith: every relationship
is counted against the live stores first, and one with so much as a single
offending row is reported with its count and left undeclared, because a
constraint the data cannot satisfy is a load that fails halfway.

They are emitted as `ALTER TABLE` after every table exists, not inline. A
table-order that satisfies every reference does not exist here — `sessions`
references itself, and the stores reference each other both ways — and
sorting tables into one would be a dependency solver nobody asked for.

Anything this module cannot translate confidently is reported, never
guessed and never skipped silently: `build()` returns the DDL and a list of
notes, and a note is a thing a person has to look at.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from . import schema_map

#: SQLite's declared type (upper-cased, stripped of size) -> Postgres type.
#: SQLite stores what it is given regardless of the declaration, so this is a
#: translation of intent. Width-carrying declarations are handled separately.
TYPES: dict[str, str] = {
    "TEXT": "text",
    "": "text",            # no declared type: SQLite's blob/any affinity
    "INTEGER": "bigint",
    "INT": "bigint",
    "BIGINT": "bigint",
    "SMALLINT": "smallint",
    "REAL": "double precision",
    "DOUBLE": "double precision",
    "FLOAT": "double precision",
    "NUMERIC": "numeric",
    "DECIMAL": "numeric",
    "BLOB": "bytea",
    "BOOLEAN": "boolean",
    "DATETIME": "timestamptz",
    "TIMESTAMP": "timestamptz",
    "DATE": "date",
    "JSON": "jsonb",
}

#: SQLite default expressions that mean something in Postgres too.
DEFAULTS: dict[str, str] = {
    "CURRENT_TIMESTAMP": "now()",
    "(CURRENT_TIMESTAMP)": "now()",
    "CURRENT_DATE": "current_date",
    "(UNIXEPOCH())": "EXTRACT(EPOCH FROM now())::bigint",
    "UNIXEPOCH()": "EXTRACT(EPOCH FROM now())::bigint",
}


class SourceUnusable(RuntimeError):
    """A source store reports nothing, so the DDL would be silently partial."""


class UndecidedTable(RuntimeError):
    """A table exists in a store and nowhere in the map."""


class DanglingReference(RuntimeError):
    """A declared foreign key points at a table the map does not create."""


class StaleDecision(RuntimeError):
    """`UNKEYED` names a column that no longer exists.

    The same reasoning as `unmapped()`: a decision nobody can check is not a
    decision. An entry whose column has been renamed or dropped documents
    nothing while still reading as deliberate.
    """


@dataclass
class Build:
    ddl: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: source -> the virtual tables and shadows left behind, per source. Not a
    #: note and not a drop: lucent's `memories_fts` is a 7,663-row search
    #: index that the "Memory search without FTS5" story replaces with
    #: pgvector and a Postgres full-text index. Reported so the omission is
    #: visible rather than inferred from a name filter.
    search_indexes: dict[str, list[str]] = field(default_factory=dict)
    #: Relationships actually declared, and the ones withheld. Reported so
    #: the schema can be checked against what the build says it did rather
    #: than against what somebody remembers deciding.
    relationships: list[schema_map.Relationship] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    #: Things this build decided, as opposed to things it could not do. Kept
    #: apart from `notes` because the exit code turns on notes: a recorded
    #: decision that never goes away would make a correct build exit non-zero
    #: forever, and an exit code that is always 1 stops being read.
    decisions: list[str] = field(default_factory=list)


def _pg_type(decl: str) -> tuple[str, str | None]:
    """Postgres type for a SQLite declared type, plus a note if it is a guess."""
    raw = (decl or "").strip()
    upper = raw.upper()
    m = re.match(r"^(VARCHAR|CHAR|CHARACTER)\s*\(\s*(\d+)\s*\)$", upper)
    if m:
        return f"varchar({m.group(2)})", None
    base = re.sub(r"\s*\(.*\)$", "", upper).strip()
    if base in TYPES:
        return TYPES[base], None
    return "text", f"unrecognised declared type {raw!r}; mapped to text"


def _default(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    v = value.strip()
    if v.upper() in DEFAULTS:
        return DEFAULTS[v.upper()], None
    # SQLite's epoch-seconds idiom. Matched by pattern rather than by an
    # upper-cased lookup, because upper-casing it would mangle the quoted
    # format string inside it.
    if re.fullmatch(r"\(?\s*strftime\(\s*'%s'\s*,\s*'now'\s*\)\s*\)?",
                    v, re.IGNORECASE):
        return "EXTRACT(EPOCH FROM now())::bigint", None
    # A plain literal — number, quoted string, NULL — passes through.
    if re.fullmatch(r"-?\d+(\.\d+)?|'[^']*'|NULL", v, re.IGNORECASE):
        return v, None
    return None, f"default {v!r} not translated; left unset"


def _columns(
    con: sqlite3.Connection, table: str, target: tuple[str, str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Column definitions, the primary-key columns, notes, and decisions."""
    rows = list(con.execute(f'PRAGMA table_info("{table}")'))
    defs: list[str] = []
    pk: list[tuple[int, str]] = []
    notes: list[str] = []
    decisions: list[str] = []
    int_pk_single = sum(1 for r in rows if r[5]) == 1
    for _cid, name, decl, notnull, dflt, pkpos in rows:
        pgtype, tnote = _pg_type(decl)
        if tnote:
            notes.append(f"{table}.{name}: {tnote}")
        # An INTEGER PRIMARY KEY in SQLite is the rowid: an autoincrementing
        # surrogate. Identity BY DEFAULT rather than ALWAYS, because the
        # loader inserts the existing ids explicitly and ALWAYS refuses that.
        if pkpos and int_pk_single and (decl or "").upper() in ("INTEGER", "INT"):
            defs.append(f'    "{name}" bigint GENERATED BY DEFAULT AS IDENTITY')
            pk.append((pkpos, name))
            continue
        piece = f'    "{name}" {pgtype}'
        if notnull:
            piece += " NOT NULL"
        dropped = schema_map.DROPPED_DEFAULTS.get((target[0], target[1], name))
        if dropped is not None:
            decisions.append(
                f"{table}.{name}: source default {dflt!r} dropped. {dropped}")
            defs.append(piece)
            if pkpos:
                pk.append((pkpos, name))
            continue
        dval, dnote = _default(dflt)
        if dval is not None:
            piece += f" DEFAULT {dval}"
        if dnote:
            notes.append(f"{table}.{name}: {dnote}")
        defs.append(piece)
        if pkpos:
            pk.append((pkpos, name))
    pkcols = [n for _, n in sorted(pk)]
    return defs, pkcols, notes, decisions


def _excluded(con: sqlite3.Connection) -> set[str]:
    """Virtual tables and their shadow tables.

    Asked of SQLite rather than guessed from names. A `name not like '%_fts%'`
    filter is wrong twice over: `_` is a single-character wildcard, so it
    swallows `drafts`, `shifts` and `gifts`; and even escaped it would exclude
    an ordinary `token_fts_cache`. Worse, a name excluded by a pattern never
    reaches the undecided-table gate, so the filter silently decided things.
    """
    virtual = [
        r[0] for r in con.execute(
            "select name from sqlite_master where type='table' "
            "and sql like 'CREATE VIRTUAL TABLE%'")
    ]
    out: set[str] = set(virtual)
    for v in virtual:
        out |= {
            r[0] for r in con.execute(
                "select name from sqlite_master where type='table' "
                "and name like ? escape '\\'",
                (v.replace("_", r"\_") + r"\_%",))
        }
    return out


def _unique_constraints(con: sqlite3.Connection, table: str) -> list[list[str]]:
    """Column lists of every UNIQUE constraint on the table.

    Read from `PRAGMA index_list`, where `origin` is `u` for a constraint and
    `c` for a standalone `CREATE INDEX`. The earlier version of this module
    reproduced only indexes whose `sqlite_master.sql` was non-NULL, and an
    inline `UNIQUE` is implemented as a `sqlite_autoindex_*` with NULL `sql` —
    so every one of them vanished, silently, including the `token_sha`
    uniqueness whose own source comment explains that losing it lets one
    holder authenticate as another.
    """
    out: list[list[str]] = []
    for row in con.execute(f'PRAGMA index_list("{table}")'):
        name, unique, origin = row[1], row[2], row[3]
        if not unique or origin != "u":
            continue
        cols = [r[2] for r in con.execute(f'PRAGMA index_info("{name}")')]
        if any(c is None for c in cols):
            continue  # expression index; carried via its own CREATE statement
        out.append(cols)
    return out


#: A table-level or column-level CHECK, as written in the source DDL.
_CHECK = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)


def _checks(con: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
    """CHECK expressions on the table, and notes for any not carried.

    No pragma reports a CHECK, so this one does read the source DDL — but it
    reads it by balancing parentheses from each `CHECK(` rather than by
    pattern-matching the expression, and anything it cannot balance is
    reported rather than dropped.
    """
    row = con.execute(
        "select sql from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    if row is None or not row[0]:
        return [], []
    sql = row[0]
    checks: list[str] = []
    notes: list[str] = []
    for m in _CHECK.finditer(sql):
        i = m.end() - 1  # at the opening paren
        depth = 0
        for j in range(i, len(sql)):
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
                if depth == 0:
                    checks.append(" ".join(sql[i:j + 1].split()))
                    break
        else:
            notes.append(f"{table}: unbalanced CHECK near offset {i}; not carried")
    return checks, notes


def _indexes(con: sqlite3.Connection, table: str, schema: str,
             new_table: str) -> tuple[list[str], list[str]]:
    """Translated standalone-index DDL, and notes for anything not translated.

    Only indexes with their own `CREATE INDEX` statement are handled here;
    UNIQUE constraints come from `_unique_constraints` and ride on the table.
    The statement's own SQL is reused rather than rebuilt from `index_info`,
    because `index_info` reports a NULL column for every expression index —
    the console's `lower(email)` uniqueness among them.
    """
    out: list[str] = []
    notes: list[str] = []
    rows = con.execute(
        "select name, sql from sqlite_master where type='index' and tbl_name=? "
        "and sql is not null", (table,)).fetchall()
    for name, sql in rows:
        stmt = " ".join(sql.split())
        m = re.match(
            r'^CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?'
            r'[`"\[]?([\w.]+)[`"\]]?\s+ON\s+[`"\[]?(\w+)[`"\]]?\s*(\(.*)$',
            stmt, re.IGNORECASE | re.DOTALL)
        if not m:
            notes.append(f"{table}: index {name} not translated: {stmt}")
            continue
        unique = "UNIQUE " if m.group(1) else ""
        body = m.group(4)
        out.append(
            f'CREATE {unique}INDEX "{schema}_{name}" '
            f'ON {schema}."{new_table}" {body};'
        )
    return out, notes



def _source_relationships(
    con: sqlite3.Connection, source: str, table: str,
) -> tuple[list[schema_map.Relationship], list[str]]:
    """Foreign keys the source file declares itself, remapped to the layout.

    Read through `PRAGMA foreign_key_list`, which reports one row per column
    of a key and groups them by its `id`. A key whose `to` column is NULL
    references the parent's primary key implicitly, and SQLite expects the
    reader to resolve that rather than reporting it.
    """
    child_target = schema_map.placement(source, table)
    if child_target is None:
        return [], []
    grouped: dict[int, list[tuple[int, str, str, str | None, str]]] = {}
    for row in con.execute(f'PRAGMA foreign_key_list("{table}")'):
        fid, seq, parent_table, from_col, to_col, _upd, on_del = row[:7]
        grouped.setdefault(fid, []).append(
            (seq, parent_table, from_col, to_col, on_del))
    out: list[schema_map.Relationship] = []
    notes: list[str] = []
    for fid in sorted(grouped):
        cols = sorted(grouped[fid])
        parent_table = cols[0][1]
        on_delete = (cols[0][4] or "NO ACTION").upper()
        parent_target = schema_map.placement(source, parent_table)
        if parent_target is None:
            raise DanglingReference(
                f"{source}.{table} declares a foreign key onto "
                f"{source}.{parent_table}, which schema_map does not create. "
                "Either that table is wrongly dropped or this key is dead — "
                "emitting it would produce DDL that cannot be applied.")
        child_cols = tuple(c[2] for c in cols)
        to_cols = [c[3] for c in cols]
        if any(c is None for c in to_cols):
            parent_cols = tuple(_primary_key(con, parent_table))
            if not parent_cols:
                notes.append(
                    f"{table}: foreign key on {child_cols} names no target "
                    f"column and {parent_table} has no primary key; withheld")
                continue
        else:
            parent_cols = tuple(str(c) for c in to_cols)
        override = schema_map.ON_DELETE_OVERRIDE.get(
            (child_target[0], child_target[1], child_cols))
        why = f"Declared by {source}.{table} itself."
        if override is not None:
            on_delete, reason = override
            why = f"{why} ON DELETE {on_delete} rather than the source's: {reason}"
        out.append(schema_map.Relationship(
            child=child_target, child_columns=child_cols,
            parent=parent_target, parent_columns=parent_cols,
            on_delete=on_delete, why=why))
    return out, notes


def _primary_key(con: sqlite3.Connection, table: str) -> list[str]:
    rows = [r for r in con.execute(f'PRAGMA table_info("{table}")') if r[5]]
    return [r[1] for r in sorted(rows, key=lambda r: r[5])]


def _origin() -> dict[tuple[str, str], tuple[str, str]]:
    """Unified table -> the source table it is built from."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for (source, table), target in schema_map.PLACEMENT.items():
        winner = schema_map.MERGE_WINNER.get(target)
        if target in out and winner != (source, table):
            continue
        out[target] = (source, table)
    for (source, table), target in schema_map.MERGES.items():
        if schema_map.MERGE_WINNER.get(target) == (source, table):
            out[target] = (source, table)
    return out


def _offenders(
    con: sqlite3.Connection, rel: schema_map.Relationship,
    origin: dict[tuple[str, str], tuple[str, str]],
) -> int | None:
    """Rows that would break this relationship, or None if it cannot be asked.

    Asked against the live stores, attached side by side, because that is the
    only place the data exists yet. A NULL child value is permitted by a
    foreign key and is not an offender; an empty string is not NULL, so it is
    one — which is the point, since Postgres will refuse it where SQLite
    never looked.
    """
    child = origin.get(rel.child)
    parent = origin.get(rel.parent)
    if child is None or parent is None:
        return None
    cs, ct = child
    ps, pt = parent
    pairs = list(zip(rel.child_columns, rel.parent_columns))
    not_null = " AND ".join(f'c."{c}" IS NOT NULL' for c, _ in pairs)
    joined = " AND ".join(f'p."{p}" = c."{c}"' for c, p in pairs)
    sql = (f'SELECT count(*) FROM "{cs}"."{ct}" c WHERE {not_null} '
           f'AND NOT EXISTS (SELECT 1 FROM "{ps}"."{pt}" p WHERE {joined})')
    try:
        return int(con.execute(sql).fetchone()[0])
    except sqlite3.Error:
        return None


def _verifier(paths: dict[str, str]) -> sqlite3.Connection:
    """One connection with every source attached under its own name."""
    con = sqlite3.connect(":memory:")
    for name, path in paths.items():
        con.execute(f'ATTACH DATABASE ? AS "{name}"', (f"file:{path}?mode=ro",))
    return con


def _constraint_ddl(rel: schema_map.Relationship) -> str:
    child_cols = ", ".join(f'"{c}"' for c in rel.child_columns)
    parent_cols = ", ".join(f'"{c}"' for c in rel.parent_columns)
    # A table referencing itself cannot be loaded in an order that satisfies
    # the key, because the order is per row rather than per table: a session
    # is inserted before the successor that rotated out of it as often as
    # after. Deferring to commit is what lets the loader insert the table in
    # one pass; every other key is satisfied by loading parents first.
    defer = (" DEFERRABLE INITIALLY DEFERRED"
             if rel.child == rel.parent else "")
    return (f'ALTER TABLE {rel.child[0]}."{rel.child[1]}" '
            f'ADD CONSTRAINT "{rel.name}" FOREIGN KEY ({child_cols}) '
            f'REFERENCES {rel.parent[0]}."{rel.parent[1]}" ({parent_cols}) '
            f'ON DELETE {rel.on_delete}{defer};')


def _check_unkeyed(columns: dict[tuple[str, str], set[str]]) -> None:
    """Every `UNKEYED` entry names a column the build actually creates."""
    stale = [
        f"{schema}.{table}.{column}"
        for (schema, table, column) in schema_map.UNKEYED
        if column not in columns.get((schema, table), set())
    ]
    if stale:
        raise StaleDecision(
            f"schema_map.UNKEYED names {stale}, which this build does not "
            "create. A reason written against a column that no longer exists "
            "reads as a decision and documents nothing.")


def _relationships(
    paths: dict[str, str], declared: list[schema_map.Relationship],
    created: set[tuple[str, str]],
) -> tuple[list[str], list[schema_map.Relationship], list[str]]:
    """The constraint DDL, what was declared, and what was withheld and why."""
    origin = _origin()
    con = _verifier(paths)
    ddl: list[str] = []
    kept: list[schema_map.Relationship] = []
    withheld: list[str] = []
    try:
        for rel in declared:
            where = f"{rel.child[0]}.{rel.child[1]}.{'/'.join(rel.child_columns)}"
            onto = f"{rel.parent[0]}.{rel.parent[1]}"
            if rel.child not in created or rel.parent not in created:
                raise DanglingReference(
                    f"{where} references {onto}, which this build does not "
                    "create.")
            bad = _offenders(con, rel, origin)
            if bad is None:
                withheld.append(
                    f"{where} -> {onto}: could not be counted against the live "
                    "stores, so it is not declared.")
                continue
            if bad:
                withheld.append(
                    f"{where} -> {onto}: {bad} row(s) point at something that "
                    "is not there, so it is not declared.")
                continue
            kept.append(rel)
            ddl.append(_constraint_ddl(rel))
    finally:
        con.close()
    for (schema, table, column), reason in sorted(schema_map.UNKEYED.items()):
        withheld.append(f"{schema}.{table}.{column}: not a reference. {reason}")
    return ddl, kept, withheld


def build(base: str) -> Build:
    """The whole DDL for the one database, in dependency-free order."""
    result = Build()
    paths = schema_map.source_paths(base)
    created: set[tuple[str, str]] = set()
    declared: list[schema_map.Relationship] = []
    columns: dict[tuple[str, str], set[str]] = {}

    # The schemas and pgvector were a hand step on the first host, which made
    # the documented `build_schema | psql` fail on its first statement against
    # a fresh database. A producer that cannot rebuild its own artifact is not
    # a producer.
    # One transaction, so a statement that fails leaves nothing behind. A
    # half-applied schema is worse than none: the re-run fails on its first
    # statement for a reason that reads as a conflict rather than as the
    # original error.
    result.ddl.append("BEGIN;")
    result.ddl.append("CREATE EXTENSION IF NOT EXISTS vector;")
    for name, purpose in schema_map.SCHEMAS.items():
        result.ddl.append(f"CREATE SCHEMA IF NOT EXISTS {name};")
        esc = purpose.replace("'", "''")
        result.ddl.append(f"COMMENT ON SCHEMA {name} IS '{esc}';")

    for source, path in paths.items():
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            skip = _excluded(con)
            tables = [
                r[0] for r in con.execute(
                    "select name from sqlite_master where type='table' "
                    "and name not like 'sqlite\\_%' escape '\\'")
                if r[0] not in skip
            ]
            result.search_indexes[source] = sorted(skip)
            if not tables:
                raise SourceUnusable(
                    f"{source} ({path}) reports no tables. SQLite opens a "
                    "zero-byte or truncated file as a valid empty database, so "
                    "this is a wrong path or a damaged store, not an empty one "
                    "— and emitting a schema without it would succeed while "
                    "silently omitting everything it holds.")
            leftover = schema_map.unmapped(source, tables)
            if leftover:
                raise UndecidedTable(
                    f"{source}: {leftover} appear in the store and nowhere in "
                    "schema_map. Decide them — place them, or drop them with a "
                    "reason — before this DDL is trusted.")
            for table in sorted(tables):
                target = schema_map.placement(source, table)
                if target is None:
                    continue
                schema, new_table = target
                # A merge target is created once, from the winner's shape.
                winner = schema_map.MERGE_WINNER.get(target)
                if winner is not None and winner != (source, table):
                    continue
                if target in created:
                    continue
                columns[target] = {
                    r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
                defs, pkcols, notes, decided = _columns(con, table, target)
                result.notes.extend(f"{source}.{n}" for n in notes)
                result.decisions.extend(f"{source}.{n}" for n in decided)
                if pkcols:
                    cols = ", ".join(f'"{c}"' for c in pkcols)
                    defs.append(f"    PRIMARY KEY ({cols})")
                for ucols in _unique_constraints(con, table):
                    cols = ", ".join(f'"{c}"' for c in ucols)
                    defs.append(f"    UNIQUE ({cols})")
                checks, cnotes = _checks(con, table)
                for expr in checks:
                    defs.append(f"    CHECK {expr}")
                result.notes.extend(f"{source}.{n}" for n in cnotes)
                result.ddl.append(
                    f'CREATE TABLE {schema}."{new_table}" (\n'
                    + ",\n".join(defs) + "\n);"
                )
                idx, inotes = _indexes(con, table, schema, new_table)
                result.ddl.extend(idx)
                result.notes.extend(f"{source}.{n}" for n in inotes)
                created.add(target)
            # Read after the tables, and for every table in the store —
            # including a merge loser, whose keys belong to the surviving
            # shape rather than disappearing with it.
            for table in sorted(tables):
                rels, rnotes = _source_relationships(con, source, table)
                declared.extend(rels)
                result.notes.extend(f"{source}.{n}" for n in rnotes)
        finally:
            con.close()

    _check_unkeyed(columns)
    declared.extend(schema_map.CROSS_FILE)
    seen: set[tuple] = set()
    unique_declared: list[schema_map.Relationship] = []
    for rel in declared:
        key = (rel.child, rel.child_columns, rel.parent, rel.parent_columns)
        if key in seen:
            continue
        seen.add(key)
        unique_declared.append(rel)
    fk_ddl, kept, withheld = _relationships(paths, unique_declared, created)
    result.ddl.extend(fk_ddl)
    result.relationships = kept
    result.withheld = withheld
    result.ddl.append("COMMIT;")
    return result


if __name__ == "__main__":
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="/home/daniel/Storage",
                    help="directory holding the Hive checkouts")
    ap.add_argument("--notes-only", action="store_true")
    args = ap.parse_args()

    import sys

    try:
        b = build(args.base)
    except (SourceUnusable, UndecidedTable, DanglingReference) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        sys.exit(2)

    if not args.notes_only:
        print("\n".join(b.ddl))
    for src, skipped in sorted(b.search_indexes.items()):
        if skipped:
            print(f"-- {src}: search index not carried (replaced by pgvector "
                  f"and Postgres full-text): {', '.join(skipped)}",
                  file=sys.stderr)
    if b.decisions:
        print(f"\n-- {len(b.decisions)} decision(s):", file=sys.stderr)
        for d in b.decisions:
            print(f"--   {d}", file=sys.stderr)
    print(f"\n-- {len(b.relationships)} relationship(s) declared:",
          file=sys.stderr)
    for rel in b.relationships:
        print(f"--   {rel.child[0]}.{rel.child[1]}"
              f".{'/'.join(rel.child_columns)} -> "
              f"{rel.parent[0]}.{rel.parent[1]} ON DELETE {rel.on_delete}",
              file=sys.stderr)
    if b.withheld:
        print(f"\n-- {len(b.withheld)} relationship(s) NOT declared:",
              file=sys.stderr)
        for w in b.withheld:
            print(f"--   {w}", file=sys.stderr)
    if b.notes:
        print(f"\n-- {len(b.notes)} note(s):", file=sys.stderr)
        for n in b.notes:
            print(f"--   {n}", file=sys.stderr)
    # A note is something a person has to look at, so it is not an exit 0.
    # The documented pipe into psql must not report success over a schema
    # whose author was warned about it.
    sys.exit(1 if b.notes else 0)
