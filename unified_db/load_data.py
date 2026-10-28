"""Load the live SQLite stores into the one database, and verify it row for row.

A one-time job, run repeatedly until it is right. It reads a *snapshot* of
each store rather than the store itself, so a load never contends with a
running service and a bad load is a retry rather than an outage. The final
load before the cutover is the same command with the services stopped — any
copy taken while they are running loses whatever was written after it.

Verification is row counts **and contents**, per table, compared against the
snapshot the load actually read. An exit code is not evidence: the whole
point of this story is that somebody can look at a table-by-table report and
see that 8,163 sessions went in and 8,163 sessions came back with the same
values in them.

`health.db` is not opened, not copied and not verified. The health app is not
Hive and its data does not belong in the database Hive takes into a corporate
setting; the empty `health` schema Postgres carries is left exactly as it is.

The shape the rows land in is `schema_map`'s, not the source's: dropped
columns are not carried, renamed columns travel under the new name, the two
integer flags become real booleans and lucent's embedding BLOB becomes a
`vector(4096)`. The generated searchable column is never written — Postgres
computes it, and naming it in an INSERT is an error.

Running it:

    python -m unified_db.load_data --base /home/daniel/Storage
    python -m unified_db.load_data --verify-only     # re-check a loaded database

Exit 0 only when every table matched. 1 when a table did not, 2 when the run
refused before loading anything.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import struct
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import asyncpg

from . import build_schema, schema_map

#: Sources this loader does not open, with the reason. A skip is named here so
#: that "it isn't in the new database" is a decision somebody can read.
SKIPPED_SOURCES: dict[str, str] = {
    "health": "hive-health is out of scope, decided 2026-10-23. The health "
              "app is not Hive. Its SQLite file is not opened and the empty "
              "`health` schema is left as it is.",
}

#: A snapshot smaller than this is a truncated copy rather than a small store.
#: SQLite opens a zero-byte file as a valid empty database, which is how a
#: load reports success over a third of a schema.
MIN_SOURCE_BYTES = 4096

#: Rows per round trip. Large enough that 143,000 usage-log rows are not
#: 143,000 round trips, small enough that one batch of memories is not 130MB
#: of embeddings in flight.
BATCH = 500


class LoadRefused(RuntimeError):
    """The run stopped before writing anything, naming what was wrong."""


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def snapshot(base: str, work: Path) -> dict[str, Path]:
    """Copy every source this loader reads into `work`, and check each one.

    Taken through SQLite's own backup API rather than `cp`, because a plain
    file copy of a database a service is writing to is a torn page, and the
    tear presents as a corrupt snapshot halfway through the load rather than
    as a failure to copy.
    """
    work.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name, path in schema_map.source_paths(base).items():
        if name in SKIPPED_SOURCES:
            continue
        src = Path(path)
        if not src.exists():
            raise LoadRefused(f"source {name!r} is not there: {src}")
        if src.stat().st_size < MIN_SOURCE_BYTES:
            raise LoadRefused(
                f"source {name!r} is {src.stat().st_size} bytes "
                f"({src}). SQLite opens a zero-byte or truncated file as a "
                "valid empty database, so this refuses rather than loading "
                "nothing and reporting success.")
        target = work / f"{name}.db"
        try:
            with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as live, \
                    sqlite3.connect(target) as copy:
                live.backup(copy)
        except sqlite3.Error as exc:
            raise LoadRefused(
                f"source {name!r} could not be read: {exc} ({src})") from exc
        with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as con:
            tables = _tables(con)
        if not tables:
            raise LoadRefused(
                f"source {name!r} reports no tables after copy ({src})")
        undecided = schema_map.unmapped(name, tables)
        if undecided:
            raise LoadRefused(
                f"source {name!r} holds table(s) the layout map says nothing "
                f"about: {', '.join(undecided)}. A table nobody has decided "
                "about is not loaded silently.")
        out[name] = target
    return out


def _tables(con: sqlite3.Connection) -> list[str]:
    """Real tables, without the virtual ones or their shadows.

    The exclusion is `build_schema`'s, imported rather than repeated: a
    second copy of that rule is a second place the set of tables is decided,
    and the one the generator does not read. lucent's `memories_fts` is the
    whole reason it exists — an FTS5 index with five shadow tables, replaced
    by a generated `tsvector` column Postgres maintains itself.
    """
    excluded = build_schema._excluded(con)
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'") if r[0] not in excluded]


# ---------------------------------------------------------------------------
# What lands where, in an order the keys allow
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """One source table and the unified table it loads into."""

    source: str
    source_table: str
    schema: str
    table: str

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


async def plan(conn: asyncpg.Connection,
               snapshots: dict[str, Path]) -> list[Target]:
    """Every table to load, parents before children.

    The order is read off the live foreign keys rather than written down: a
    key added to the schema later would otherwise make the load fail halfway
    on a constraint nobody had thought to reorder for. Self-references are
    ignored here — the schema declares those deferrable, so one pass per
    table is enough.
    """
    targets: list[Target] = []
    for source, path in sorted(snapshots.items()):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            for table in sorted(_tables(con)):
                placed = schema_map.placement(source, table)
                if placed is None:
                    continue
                targets.append(Target(source, table, placed[0], placed[1]))

    edges = await conn.fetch("""
        SELECT cs.nspname AS child_schema, ct.relname AS child_table,
               ps.nspname AS parent_schema, pt.relname AS parent_table
        FROM pg_constraint c
        JOIN pg_class ct ON ct.oid = c.conrelid
        JOIN pg_namespace cs ON cs.oid = ct.relnamespace
        JOIN pg_class pt ON pt.oid = c.confrelid
        JOIN pg_namespace ps ON ps.oid = pt.relnamespace
        WHERE c.contype = 'f'
    """)
    parents: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for t in targets:
        parents.setdefault((t.schema, t.table), set())
    for e in edges:
        child = (e["child_schema"], e["child_table"])
        parent = (e["parent_schema"], e["parent_table"])
        if child == parent or child not in parents or parent not in parents:
            continue
        parents[child].add(parent)

    ordered: list[Target] = []
    placed: set[tuple[str, str]] = set()
    remaining = list(targets)
    while remaining:
        ready = [t for t in remaining
                 if parents[(t.schema, t.table)] <= placed]
        if not ready:
            stuck = ", ".join(sorted(t.qualified for t in remaining))
            raise LoadRefused(
                "the foreign keys between these tables form a cycle this "
                f"loader cannot order: {stuck}. A cycle needs its keys "
                "declared deferrable before the data can go in.")
        for t in ready:
            ordered.append(t)
            placed.add((t.schema, t.table))
        remaining = [t for t in remaining if t not in ready]
    return ordered


# ---------------------------------------------------------------------------
# Reading a source row into the shape the target declares
# ---------------------------------------------------------------------------

@dataclass
class ColumnShape:
    """One target column, and the source column behind it."""

    name: str
    source_name: str
    udt: str
    generated: bool


async def columns(conn: asyncpg.Connection, target: Target) -> list[ColumnShape]:
    rows = await conn.fetch("""
        SELECT column_name, udt_name, is_generated
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position
    """, target.schema, target.table)
    if not rows:
        raise LoadRefused(
            f"{target.qualified} is not in the database. Apply the schema "
            "before loading.")
    shapes: list[ColumnShape] = []
    for r in rows:
        shapes.append(ColumnShape(
            name=r["column_name"],
            source_name=_source_name(target, r["column_name"]),
            udt=r["udt_name"],
            generated=r["is_generated"] == "ALWAYS",
        ))
    return shapes


def _source_name(target: Target, column: str) -> str:
    for (schema, table, src), (new, _why) in schema_map.COLUMN_RENAMES.items():
        if (schema, table) == (target.schema, target.table) and new == column:
            return src
    return column


_NAIVE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$")


#: Conversions that lose something, recorded per column rather than per row.
#: A lossy conversion that verification then calls a match is only honest if
#: the loss is on the report somebody reads.
LOSSY: dict[str, str] = {}


def convert(value: object, udt: str, where: str) -> object:
    """A SQLite value as the Postgres type the map declares for it.

    Every conversion here is one the source could not express. SQLite has no
    boolean, no timestamp and no vector, so a flag arrived as 0, an instant
    as text and a 4096-float embedding as 16,384 bytes.
    """
    if value is None:
        return None
    if udt == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str) and value.strip() in ("0", "1"):
            return value.strip() == "1"
        raise LoadRefused(
            f"{where}: {value!r} is not a flag value for a boolean column")
    if udt == "vector":
        return _vector(value, where)
    if udt == "timestamptz":
        return _instant(value, where)
    if udt == "numeric":
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (int, str)):
            return Decimal(str(value))
        if isinstance(value, float):
            return Decimal(repr(value))
        raise LoadRefused(f"{where}: {value!r} is not a number")
    if udt in ("int8", "int4", "int2"):
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            # SQLite does not enforce a declared type, so a column declared
            # INTEGER can hold a float — `memories.created_at` does, on 53 of
            # 7,970 rows, as epoch seconds with a fraction. The declaration is
            # what every reader of it means, so the fraction goes rather than
            # the row, and the column is named on the report.
            if value != int(value):
                LOSSY.setdefault(
                    where,
                    "a float in a column declared INTEGER; the fractional "
                    "part is not carried")
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        raise LoadRefused(f"{where}: {value!r} is not an integer")
    if udt == "float8":
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value)
        raise LoadRefused(f"{where}: {value!r} is not a number")
    if udt == "bytea":
        return value if isinstance(value, bytes) else bytes(str(value), "utf-8")
    # text and varchar. A column SQLite left untyped carries whatever was
    # written to it, so an integer in a text column is coerced rather than
    # refused — but a BLOB is not, since str(b'...') would store the repr.
    if isinstance(value, bytes):
        raise LoadRefused(
            f"{where}: a {len(value)}-byte BLOB in a text column. The map "
            "types this column text; a blob there is a decision somebody "
            "has to make, not a coercion.")
    return value if isinstance(value, str) else str(value)


def _vector(value: object, where: str) -> list[float]:
    """lucent's stored embedding as a list of floats.

    `np.float32(...).tobytes()` is what lucent writes, with a JSON text
    fallback its own reader still tolerates. Anything else refuses: a blob
    whose length merely divides by four would decode into a plausible vector
    of the wrong numbers.
    """
    if isinstance(value, bytes):
        if len(value) % 4 != 0:
            raise LoadRefused(
                f"{where}: {len(value)} bytes is not a whole number of "
                "float32s")
        count = len(value) // 4
        return list(struct.unpack(f"<{count}f", value))
    if isinstance(value, str):
        import json  # noqa: PLC0415
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise LoadRefused(f"{where}: not a vector: {exc}") from exc
        return [float(x) for x in parsed]
    raise LoadRefused(f"{where}: {type(value).__name__} is not an embedding")


def _instant(value: object, where: str) -> datetime:
    """A SQLite timestamp as an instant.

    The sources write `CURRENT_TIMESTAMP`, which SQLite renders in UTC, so
    UTC is attached here explicitly rather than left to the server's own
    `TimeZone`. A naive string inserted into `timestamptz` is read in
    whatever zone the server happens to be set to, which makes the instants
    depend on a setting nobody loading data is looking at.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and _NAIVE.match(value.strip()):
        return datetime.fromisoformat(
            value.strip().replace(" ", "T")).replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip())
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise LoadRefused(f"{where}: {value!r} is not a timestamp")


# ---------------------------------------------------------------------------
# The load
# ---------------------------------------------------------------------------

@dataclass
class TableResult:
    target: Target
    source_rows: int = 0
    target_rows: int = 0
    contents: str = "not checked"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return (self.source_rows == self.target_rows
                and self.contents == "match")


@dataclass
class Report:
    tables: list[TableResult] = field(default_factory=list)
    sequences: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.tables) and all(t.ok for t in self.tables)


async def _register_vector(conn: asyncpg.Connection) -> None:
    """Teach this connection pgvector's text form.

    Text rather than binary: the width here is 4096, there is no approximate
    index at that width, and this runs once. The encoder is what makes a
    list of floats insertable; the decoder is what makes verification able to
    compare the stored vector against the bytes it came from.
    """
    await conn.set_type_codec(
        "vector",
        schema="public",
        encoder=lambda v: "[" + ",".join(repr(float(x)) for x in v) + "]",
        decoder=lambda s: [float(x) for x in s.strip()[1:-1].split(",")] if s.strip()[1:-1] else [],
        format="text",
    )


async def load(conn: asyncpg.Connection, snapshots: dict[str, Path],
               report: Report) -> None:
    """Empty every table this loader owns, then fill it from the snapshot.

    Emptying first is what makes a second run leave the same database as the
    first rather than a doubled one. It is one `TRUNCATE` naming every table,
    which is how Postgres allows it despite the keys between them, and
    without `CASCADE` — a key reaching in from a table this loader does not
    own should stop the run, not be followed.
    """
    targets = await plan(conn, snapshots)
    names = ", ".join(f'"{t.schema}"."{t.table}"' for t in targets)
    async with conn.transaction():
        await conn.execute(f"TRUNCATE {names} RESTART IDENTITY")
        for target in targets:
            shapes = await columns(conn, target)
            writable = [s for s in shapes if not s.generated]
            await _fill(conn, target, writable, snapshots[target.source])
    for target in targets:
        await _setval(conn, target, report)


async def _fill(conn: asyncpg.Connection, target: Target,
                shapes: list[ColumnShape], snapshot_path: Path) -> None:
    with sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True) as con:
        present = {r[1] for r in con.execute(
            f'PRAGMA table_info("{target.source_table}")')}
        missing = [s.source_name for s in shapes if s.source_name not in present]
        if missing:
            raise LoadRefused(
                f"{target.qualified}: the source does not carry "
                f"{', '.join(missing)}. The column shape in the map and the "
                "store have diverged.")
        picked = [s for s in shapes if s.source_name in present]
        select = ", ".join(f'"{s.source_name}"' for s in picked)
        columns_sql = ", ".join(f'"{s.name}"' for s in picked)
        params = ", ".join(f"${i}" for i in range(1, len(picked) + 1))
        insert = (f'INSERT INTO "{target.schema}"."{target.table}" '
                  f"({columns_sql}) VALUES ({params})")
        cursor = con.execute(
            f'SELECT {select} FROM "{target.source_table}"')
        batch: list[tuple] = []
        while True:
            rows = cursor.fetchmany(BATCH)
            if not rows:
                break
            for row in rows:
                batch.append(tuple(
                    convert(value, s.udt,
                            f"{target.qualified}.{s.name}")
                    for value, s in zip(row, picked)))
            await conn.executemany(insert, batch)
            batch = []


async def _setval(conn: asyncpg.Connection, target: Target,
                  report: Report) -> None:
    """Move every identity counter past the rows just loaded.

    Every surrogate key is `GENERATED BY DEFAULT AS IDENTITY` so this loader
    can insert the ids it was given. The counter does not follow an explicit
    insert, so without this the first application insert after the cutover
    collides on the primary key.
    """
    identity = await conn.fetch("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
          AND is_identity = 'YES'
    """, target.schema, target.table)
    for row in identity:
        column = row["column_name"]
        sequence = await conn.fetchval(
            "SELECT pg_get_serial_sequence($1, $2)",
            f'"{target.schema}"."{target.table}"', column)
        if sequence is None:
            report.notes.append(
                f"{target.qualified}.{column} is an identity column with no "
                "sequence behind it; nothing to set")
            continue
        top = await conn.fetchval(
            f'SELECT max("{column}") FROM "{target.schema}"."{target.table}"')
        if top is None:
            await conn.execute("SELECT setval($1, 1, false)", sequence)
            report.sequences.append(f"{target.qualified}.{column} -> 1 (empty)")
            continue
        await conn.execute("SELECT setval($1, $2, true)", sequence, int(top))
        report.sequences.append(f"{target.qualified}.{column} -> {top}")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

async def verify(conn: asyncpg.Connection, snapshots: dict[str, Path],
                 report: Report) -> None:
    """Row counts and contents, per table, against the snapshot that was read.

    Contents, not just counts: a load that wrote the right number of rows
    with a column shifted by one is a successful-looking load, and a count
    cannot tell. Every value read back out of Postgres is compared against
    the source value converted the way the load converted it, so what this
    proves is that the row in the database is the row in the store.
    """
    targets = await plan(conn, snapshots)
    for target in targets:
        result = TableResult(target)
        report.tables.append(result)
        shapes = await columns(conn, target)
        readable = [s for s in shapes if not s.generated]
        key = await _order_key(conn, target, readable)

        with sqlite3.connect(f"file:{snapshots[target.source]}?mode=ro",
                             uri=True) as con:
            result.source_rows = con.execute(
                f'SELECT count(*) FROM "{target.source_table}"').fetchone()[0]
            result.target_rows = await conn.fetchval(
                f'SELECT count(*) FROM "{target.schema}"."{target.table}"')
            if result.source_rows != result.target_rows:
                result.contents = "not checked"
                result.detail = "row counts differ; contents not compared"
                continue

            select_src = ", ".join(f'"{s.source_name}"' for s in readable)
            order_src = ", ".join(f'"{_src(readable, c)}"' for c in key)
            select_tgt = ", ".join(f'"{s.name}"' for s in readable)
            # `COLLATE "C"` on every text ordering column, because the two
            # engines do not sort text the same way. SQLite compares bytes;
            # Postgres sorts under the database's own collation, which on this
            # server puts 'Organization' before 'OS' and ignores the hyphens
            # inside a conversation id. Two correct orderings that differ make
            # every row after the first divergence read as a content
            # mismatch, which is a verification that reports a corrupt load
            # over a perfect one.
            order_tgt = ", ".join(
                f'"{c}" COLLATE "C"' if _is_text(readable, c) else f'"{c}"'
                for c in key)
            cursor = con.execute(
                f'SELECT {select_src} FROM "{target.source_table}" '
                f"ORDER BY {order_src}")
            offset = 0
            mismatch = ""
            while not mismatch:
                source_rows = cursor.fetchmany(BATCH)
                if not source_rows:
                    break
                target_rows = await conn.fetch(
                    f'SELECT {select_tgt} FROM "{target.schema}"."{target.table}" '
                    f"ORDER BY {order_tgt} LIMIT {len(source_rows)} OFFSET {offset}")
                offset += len(source_rows)
                for index, (src_row, tgt_row) in enumerate(
                        zip(source_rows, target_rows)):
                    for value, shape in zip(src_row, readable):
                        expected = convert(
                            value, shape.udt,
                            f"{target.qualified}.{shape.name}")
                        got = tgt_row[shape.name]
                        if not _same(expected, got):
                            mismatch = (
                                f"row {offset - len(source_rows) + index + 1}, "
                                f"column {shape.name}: store has "
                                f"{_short(expected)}, database has "
                                f"{_short(got)}")
                            break
                    if mismatch:
                        break
        if mismatch:
            result.contents = "mismatch"
            result.detail = mismatch
        else:
            result.contents = "match"


async def _order_key(conn: asyncpg.Connection, target: Target,
                     readable: list[ColumnShape]) -> list[str]:
    """Columns that put both sides in the same order.

    The primary key when there is one. Otherwise every column, which is a
    total order on any table whose rows are distinguishable at all, and the
    only honest alternative to comparing two arbitrary orderings.
    """
    rows = await conn.fetch("""
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid
                           AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = $1::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
    """, f'"{target.schema}"."{target.table}"')
    key = [r["attname"] for r in rows]
    if key:
        return key
    return [s.name for s in readable if s.udt != "vector"]


def _src(readable: list[ColumnShape], column: str) -> str:
    for s in readable:
        if s.name == column:
            return s.source_name
    return column


def _is_text(readable: list[ColumnShape], column: str) -> bool:
    return any(s.name == column and s.udt in ("text", "varchar", "bpchar")
               for s in readable)


def _same(expected: object, got: object) -> bool:
    if isinstance(expected, list) and isinstance(got, list):
        return (len(expected) == len(got)
                and all(struct.pack("<f", a) == struct.pack("<f", b)
                        for a, b in zip(expected, got)))
    if isinstance(expected, Decimal) and isinstance(got, Decimal):
        return expected == got
    if isinstance(expected, float) and isinstance(got, float):
        return struct.pack("<d", expected) == struct.pack("<d", got)
    return expected == got


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(base: str, dsn: str, *, verify_only: bool = False,
              work: Path | None = None, keep: bool = False,
              reuse: Path | None = None) -> Report:
    """Snapshot, load, verify — against one snapshot, start to finish.

    `reuse` names a snapshot a previous run kept. Verification has to be
    against the copy the load actually read: taking a fresh one first would
    compare the database against a store that has been written to since, and
    report every table the hive is busiest in as corrupt.
    """
    report = Report()
    LOSSY.clear()
    for name, why in sorted(SKIPPED_SOURCES.items()):
        report.notes.append(f"{name}: not loaded. {why}")
    directory = reuse or work or Path(tempfile.mkdtemp(prefix="hive-load-"))
    # 0700: a snapshot is every memory, every message and every credential
    # digest in the hive. World-readable copies of that under /tmp is how a
    # migration leaks a store it never wrote to.
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    try:
        if reuse is not None:
            snapshots = {name: reuse / f"{name}.db"
                         for name in schema_map.SOURCES
                         if name not in SKIPPED_SOURCES}
            absent = [str(p) for p in snapshots.values() if not p.exists()]
            if absent:
                raise LoadRefused(
                    f"the snapshot at {reuse} is missing {', '.join(absent)}")
            report.notes.append(f"verified against the snapshot at {reuse}")
        else:
            snapshots = snapshot(base, directory)
        conn = await asyncpg.connect(dsn)
        try:
            await _register_vector(conn)
            if not verify_only:
                await load(conn, snapshots, report)
            await verify(conn, snapshots, report)
        finally:
            await conn.close()
    finally:
        if not keep and reuse is None:
            shutil.rmtree(directory, ignore_errors=True)
        else:
            report.notes.append(f"snapshot kept at {directory}")
    for where, why in sorted(LOSSY.items()):
        report.notes.append(f"{where}: {why}")
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="/home/daniel/Storage",
                    help="directory holding the Hive checkouts")
    ap.add_argument("--dsn", default=os.environ.get("HIVE_DATABASE_URL", ""),
                    help="Postgres URL (default: $HIVE_DATABASE_URL)")
    ap.add_argument("--verify-only", action="store_true",
                    help="check a database that is already loaded")
    ap.add_argument("--keep", action="store_true",
                    help="leave the snapshot on disk")
    ap.add_argument("--snapshot", default=None,
                    help="verify against a snapshot a previous run kept")
    args = ap.parse_args(argv)
    if not args.dsn:
        print("REFUSED: no database named. Set HIVE_DATABASE_URL or pass "
              "--dsn.", file=sys.stderr)
        return 2

    if args.verify_only and not args.snapshot:
        print("REFUSED: --verify-only needs --snapshot naming the copy the "
              "load read. A fresh snapshot of a live store is not what is in "
              "the database.", file=sys.stderr)
        return 2

    try:
        report = asyncio.run(run(args.base, args.dsn,
                                 verify_only=args.verify_only,
                                 keep=args.keep,
                                 reuse=Path(args.snapshot) if args.snapshot
                                 else None))
    except LoadRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError, asyncpg.PostgresError) as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    width = max((len(t.target.qualified) for t in report.tables), default=0)
    for t in sorted(report.tables, key=lambda r: r.target.qualified):
        flag = "ok  " if t.ok else "FAIL"
        line = (f"{flag} {t.target.qualified:<{width}}  "
                f"{t.source_rows:>7} rows -> {t.target_rows:>7}  "
                f"{t.contents}")
        print(line if not t.detail else f"{line}\n       {t.detail}")
    print(f"\n{len(report.tables)} table(s), "
          f"{sum(t.source_rows for t in report.tables)} row(s), "
          f"{sum(1 for t in report.tables if not t.ok)} not matching")
    for note in report.notes:
        print(f"note: {note}")
    if report.sequences:
        print(f"{len(report.sequences)} identity counter(s) set")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
