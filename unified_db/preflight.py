"""Ask the loaded database the questions the SQLite files answer today.

Fired before the cutover, against a database `load_data` has just filled.
Row counts and contents prove the *data* arrived; this proves the database
can be *read*, by putting the same question to both engines and comparing
the answers. A load can be byte-perfect and still fail here — a missing
index makes a query time out, a collation puts a session picker in a
different order, a generated column nobody populated returns no search
hits.

Reported per area, pass or fail, with what differed. Nothing here is a unit
test: there is no behaviour to regress, there is one database to inspect
before it is trusted.

It writes, in a transaction it rolls back. An identity counter that was
never moved past the loaded rows is invisible to every read-only check and
collides on the first insert after the cutover, so the only honest way to
check it is to insert a row and look at the id.

    python -m unified_db.preflight --snapshot <dir>

Exit 0 only when every area passed.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

from . import load_data, schema_map


@dataclass
class AreaResult:
    area: str
    ok: bool
    detail: str = ""


@dataclass
class Preflight:
    areas: list[AreaResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.areas) and all(a.ok for a in self.areas)

    def record(self, area: str, ok: bool, detail: str = "") -> None:
        self.areas.append(AreaResult(area, ok, detail))


def _sqlite(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _diff(name: str, left: object, right: object) -> str:
    return f"{name}: store {left!r}, database {right!r}"


# ---------------------------------------------------------------------------
# One area per question somebody actually asks the database
# ---------------------------------------------------------------------------

async def _minds(conn: asyncpg.Connection, snap: Path) -> tuple[bool, str]:
    """The gateway's mind listing: who exists, on what, for whom."""
    with _sqlite(snap / "broker.db") as con:
        want = sorted(
            (r["mind_id"], r["name"], r["owner"], r["model"], r["harness"])
            for r in con.execute(
                "SELECT mind_id, name, owner, model, harness FROM minds"))
    rows = await conn.fetch(
        "SELECT mind_id, name, owner, model, harness FROM core.minds")
    got = sorted((r["mind_id"], r["name"], r["owner"], r["model"],
                  r["harness"]) for r in rows)
    if want != got:
        only_store = [m for m in want if m not in got]
        only_db = [m for m in got if m not in want]
        return False, _diff("minds", only_store[:2], only_db[:2])
    return True, f"{len(got)} mind(s), identical"


async def _session_picker(conn: asyncpg.Connection,
                          snap: Path) -> tuple[bool, str]:
    """The session picker: the most recent conversations for one mind.

    The query the console and every surface run, including the ORDER BY. A
    picker that lists the same rows in a different order is a picker that
    shows somebody the wrong conversation at the top.
    """
    with _sqlite(snap / "sessions.db") as con:
        mind = con.execute(
            "SELECT mind_id FROM sessions GROUP BY mind_id "
            "ORDER BY count(*) DESC LIMIT 1").fetchone()
        if mind is None:
            return True, "no sessions to pick from"
        mind_id = mind["mind_id"]
        want = [(r["id"], r["owner_type"], r["name"], r["last_active"])
                for r in con.execute(
                    "SELECT id, owner_type, name, last_active FROM sessions "
                    "WHERE mind_id = ? ORDER BY last_active DESC, id ASC "
                    "LIMIT 50", (mind_id,))]
    rows = await conn.fetch(
        "SELECT id, surface, name, last_active FROM core.sessions "
        "WHERE mind_id = $1 ORDER BY last_active DESC, id ASC LIMIT 50",
        mind_id)
    got = [(r["id"], r["surface"], r["name"], r["last_active"]) for r in rows]
    if want != got:
        for index, (a, b) in enumerate(zip(want, got)):
            if a != b:
                return False, _diff(f"session {index + 1} of 50", a, b)
        return False, _diff("picker length", len(want), len(got))
    return True, f"top {len(got)} for {mind_id}, same rows in the same order"


async def _memory_shape(conn: asyncpg.Connection,
                        snap: Path) -> tuple[bool, str]:
    """Memory, counted the way every recall scopes it, and the embeddings.

    Keyword *ranking* is not compared here: `ts_rank` is a different formula
    from BM25 and the port never claimed otherwise. What must agree is which
    memories exist, who they belong to, and that the 4096 floats behind each
    one survived the trip out of a BLOB.
    """
    with _sqlite(snap / "lucent.db") as con:
        want = sorted((r["mind_id"], r["data_class"], r["tier"], r["n"])
                      for r in con.execute(
                          "SELECT mind_id, data_class, tier, count(*) AS n "
                          "FROM memories GROUP BY 1, 2, 3"))
        sample = con.execute(
            "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL "
            "ORDER BY id DESC LIMIT 25").fetchall()
    rows = await conn.fetch(
        "SELECT mind_id, data_class, tier, count(*) AS n FROM memory.memories "
        "GROUP BY 1, 2, 3")
    got = sorted((r["mind_id"], r["data_class"], r["tier"], r["n"])
                 for r in rows)
    if want != got:
        only_store = [g for g in want if g not in got]
        return False, _diff("memory scoping", only_store[:2],
                            [g for g in got if g not in want][:2])
    for row in sample:
        expected = load_data.convert(row["embedding"], "vector",
                                     "memory.memories.embedding")
        stored = await conn.fetchval(
            "SELECT embedding FROM memory.memories WHERE id = $1", row["id"])
        if stored is None:
            return False, f"memory {row['id']} has no embedding in the database"
        if not load_data._same(expected, stored):
            return False, (f"memory {row['id']}: embedding differs at "
                           f"{len(expected)} vs {len(stored)} values")
    return True, (f"{len(got)} scoping group(s) identical, "
                  f"{len(sample)} embeddings exact")

async def _search_column(conn: asyncpg.Connection,
                         snap: Path) -> tuple[bool, str]:
    """The generated searchable column, which no load writes.

    Postgres computes it. A memory with content and an empty `content_search`
    means the column was created after the rows went in, or the text
    configuration is not what the index was built with — and the symptom is a
    keyword search that silently finds nothing.
    """
    # An empty vector is not in itself wrong: three live memories are a bare
    # `{` or `:[`, which hold no word to index under any configuration. What
    # would be wrong is content that *does* parse into lexemes sitting behind
    # an empty column, which is what a column added after the rows, or built
    # with a different configuration, looks like.
    config = f"{schema_map.TEXT_CONFIG_SCHEMA}.{schema_map.TEXT_CONFIG}"
    missing = await conn.fetchval("""
        SELECT count(*) FROM memory.memories
        WHERE (content_search IS NULL OR content_search = ''::tsvector)
          AND to_tsvector($1::regconfig, content) <> ''::tsvector
    """, config)
    if missing:
        return False, (f"{missing} memory(ies) whose words are indexable and "
                       "whose search vector is empty")
    hits = await conn.fetchval("""
        SELECT count(*) FROM memory.memories
        WHERE content_search @@ to_tsquery($1::regconfig, 'rotation')
    """, config)
    total = await conn.fetchval("SELECT count(*) FROM memory.memories")
    if not hits:
        return False, ("the search index answers nothing for a word the live "
                       "store holds")
    return True, f"{total} memory(ies) indexed, {hits} match a known word"


async def _graph(conn: asyncpg.Connection, snap: Path) -> tuple[bool, str]:
    """The knowledge graph, walked one hop the way `/graph/neighbors` does."""
    with _sqlite(snap / "lucent.db") as con:
        want_nodes = sorted((r["type"], r["n"]) for r in con.execute(
            "SELECT type, count(*) AS n FROM nodes GROUP BY 1"))
        busiest = con.execute(
            "SELECT source_id FROM edges GROUP BY source_id "
            "ORDER BY count(*) DESC LIMIT 1").fetchone()
        want_edges = []
        if busiest is not None:
            want_edges = sorted(
                (r["target_id"], r["type"]) for r in con.execute(
                    "SELECT target_id, type FROM edges WHERE source_id = ?",
                    (busiest["source_id"],)))
    rows = await conn.fetch(
        "SELECT type, count(*) AS n FROM memory.nodes GROUP BY 1")
    got_nodes = sorted((r["type"], r["n"]) for r in rows)
    if want_nodes != got_nodes:
        return False, _diff("node types", want_nodes, got_nodes)
    if busiest is not None:
        rows = await conn.fetch(
            "SELECT target_id, type FROM memory.edges WHERE source_id = $1",
            busiest["source_id"])
        got_edges = sorted((r["target_id"], r["type"]) for r in rows)
        if want_edges != got_edges:
            return False, _diff(f"neighbours of {busiest['source_id']}",
                                len(want_edges), len(got_edges))
    return True, (f"{len(got_nodes)} node type(s), "
                  f"{len(want_edges)} neighbour(s) of the busiest node")


async def _board(conn: asyncpg.Connection, snap: Path) -> tuple[bool, str]:
    """The board's tree, which is only a tree if parent and position survived."""
    with _sqlite(snap / "console.db") as con:
        want = [(r["id"], r["kind"], r["parent_id"], r["position"], r["status"])
                for r in con.execute(
                    "SELECT id, kind, parent_id, position, status "
                    "FROM work_items ORDER BY position, rowid")]
    rows = await conn.fetch(
        "SELECT id, kind, parent_id, position, status FROM board.work_items "
        "ORDER BY position, id")
    got = {(r["id"], r["kind"], r["parent_id"], r["position"], r["status"])
           for r in rows}
    if set(want) != got:
        only_store = [w for w in want if w not in got]
        return False, _diff("work items", only_store[:1],
                            [g for g in got if g not in set(want)][:1])
    orphans = await conn.fetchval("""
        SELECT count(*) FROM board.work_items c
        WHERE c.parent_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM board.work_items p WHERE p.id = c.parent_id)
    """)
    if orphans:
        return False, f"{orphans} work item(s) point at a missing parent"
    return True, f"{len(got)} work item(s), no orphans"


async def _tools(conn: asyncpg.Connection, snap: Path) -> tuple[bool, str]:
    """hive-tools' approval queue and its tokens."""
    with _sqlite(snap / "tools.db") as con:
        want_hitl = sorted((r["status"], r["n"]) for r in con.execute(
            "SELECT status, count(*) AS n FROM hitl_requests GROUP BY 1"))
        want_settings = sorted((r["tool_name"], r["mode"]) for r in con.execute(
            "SELECT tool_name, mode FROM tool_hitl_settings"))
    rows = await conn.fetch(
        "SELECT status, count(*) AS n FROM tools.hitl_requests GROUP BY 1")
    got_hitl = sorted((r["status"], r["n"]) for r in rows)
    rows = await conn.fetch("SELECT tool_name, mode FROM tools.tool_hitl_settings")
    got_settings = sorted((r["tool_name"], r["mode"]) for r in rows)
    if want_hitl != got_hitl:
        return False, _diff("approvals by status", want_hitl, got_hitl)
    if want_settings != got_settings:
        return False, _diff("per-tool approval mode",
                            len(want_settings), len(got_settings))
    return True, (f"{len(got_hitl)} status group(s), "
                  f"{len(got_settings)} tool setting(s)")


async def _proxy(conn: asyncpg.Connection, snap: Path) -> tuple[bool, str]:
    """The proxy's metering, summed the way its usage page sums it."""
    with _sqlite(snap / "proxy.db") as con:
        want = sorted(
            (r["model_name"], r["calls"], r["tokens"]) for r in con.execute(
                "SELECT model_name, count(*) AS calls, "
                "coalesce(sum(input_tokens + output_tokens), 0) AS tokens "
                "FROM usage_log GROUP BY 1"))
        want_models = sorted(
            (r["deployment_name"], bool(r["enabled"]), r["context_window"],
             r["rotation_threshold_percent"])
            for r in con.execute(
                "SELECT deployment_name, enabled, context_window, "
                "rotation_threshold_percent FROM models"))
    rows = await conn.fetch(
        "SELECT model_name, count(*) AS calls, "
        "coalesce(sum(input_tokens + output_tokens), 0) AS tokens "
        "FROM proxy.usage_log GROUP BY 1")
    got = sorted((r["model_name"], r["calls"], int(r["tokens"]))
                 for r in rows)
    if want != got:
        only_store = [w for w in want if w not in got]
        return False, _diff("usage by model", only_store[:2],
                            [g for g in got if g not in want][:2])
    rows = await conn.fetch(
        "SELECT deployment_name, enabled, context_window, "
        "rotation_threshold_percent FROM proxy.models")
    got_models = sorted(
        (r["deployment_name"], r["enabled"], r["context_window"],
         r["rotation_threshold_percent"]) for r in rows)
    if want_models != got_models:
        return False, _diff("models", want_models[:2], got_models[:2])
    return True, (f"{len(got)} model(s) metered, "
                  f"{sum(c for _, c, _ in got)} call(s) accounted for")


async def _keys(conn: asyncpg.Connection, _snap: Path) -> tuple[bool, str]:
    """Every declared foreign key, validated against what is now in there.

    The load satisfied them row by row as it inserted. This asks Postgres to
    say so about the whole table at once, which is the only statement that
    covers a constraint marked valid without ever having been checked.
    """
    constraints = await conn.fetch("""
        SELECT n.nspname AS schema, t.relname AS table, c.conname AS name,
               c.convalidated
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE c.contype = 'f' AND n.nspname <> 'health'
    """)
    unvalidated = [f"{r['schema']}.{r['table']}.{r['name']}"
                   for r in constraints if not r["convalidated"]]
    if unvalidated:
        return False, f"not validated: {', '.join(unvalidated)}"
    return True, f"{len(constraints)} foreign key(s), all validated"


class _Rollback(RuntimeError):
    """Raised to end the write check's transaction. Never escapes."""


async def _write_path(conn: asyncpg.Connection,
                      _snap: Path) -> tuple[bool, str]:
    """Check every identity counter, and insert one row to prove the path.

    Every surrogate key is `GENERATED BY DEFAULT AS IDENTITY`, which lets the
    loader supply existing ids and leaves the counter where it was. An
    application insert omits the id, so a counter still sitting behind the
    loaded rows collides on the first write after the cutover — and no read
    can tell.

    The counters are *read*, never touched. `pg_sequence_last_value` answers
    the question without consuming anything, which matters three ways. An
    earlier version called `nextval` and then put the counter back with
    `setval(max(id))`, which meant the check repaired the very fault it was
    reporting — so the first run failed, the second passed with nothing
    changed in between, and the whole thing read like flaky infrastructure.
    It also *lowered* a counter legitimately ahead of its rows: every one of
    these tables is mostly holes, and `core.messages.recipient_session_id`
    and `board.capability_node_settings.node_id` are documented as holding
    ids of rows that are gone, so a reused id makes a stale reference
    silently resolve to a brand-new unrelated row. And it read `max(id)`
    before writing, so a row inserted in between left the counter behind it.

    A counter *ahead* of its rows is correct. Only one behind them is a
    fault, and this never writes either way.
    """
    behind: list[str] = []
    checked = 0
    rows = await conn.fetch("""
        SELECT c.table_schema AS schema, c.table_name AS table,
               c.column_name AS column
        FROM information_schema.columns c
        WHERE c.is_identity = 'YES' AND c.table_schema <> 'health'
        ORDER BY 1, 2
    """)
    for row in rows:
        qualified = f'"{row["schema"]}"."{row["table"]}"'
        top = await conn.fetchval(
            f'SELECT max("{row["column"]}") FROM {qualified}')
        if top is None:
            continue
        sequence = await conn.fetchval(
            "SELECT pg_get_serial_sequence($1, $2)",
            f'{row["schema"]}.{row["table"]}', row["column"])
        if sequence is None:
            behind.append(f"{row['schema']}.{row['table']}.{row['column']}: "
                          "an identity column with no sequence behind it")
            continue
        last = await conn.fetchval(
            "SELECT pg_sequence_last_value($1::regclass)", sequence)
        checked += 1
        # A sequence nothing has drawn from answers NULL, which means the
        # next value is its start — 1 — and every loaded row is above it.
        if last is None or last < top:
            behind.append(
                f"{row['schema']}.{row['table']}.{row['column']}: the "
                f"counter is at {'unused' if last is None else last} with "
                f"{top} already loaded; the first insert collides")

    # And one real insert, through the table with the most identity traffic.
    # The sequence it draws from is not put back: a sequence is not
    # transactional, so the rolled-back row leaves a gap of one. A gap is
    # harmless — a collision is the fault, and only an insert can see it.
    new_id: int | None = None
    indexed = False
    try:
        async with conn.transaction():
            new_id = await conn.fetchval("""
                INSERT INTO memory.memories (mind_id, content, data_class,
                                             tier, source, created_at)
                VALUES ('preflight', 'preflight write check', 'current-state',
                        'contextual', 'tool', 0)
                RETURNING id
            """)
            indexed = await conn.fetchval(
                "SELECT content_search IS NOT NULL FROM memory.memories "
                "WHERE id = $1", new_id)
            raise _Rollback
    except _Rollback:
        pass
    top = await conn.fetchval("SELECT max(id) FROM memory.memories")
    if new_id is None:
        behind.append("an insert into memory.memories returned no id")
    elif top is not None and new_id <= top:
        behind.append(f"an insert took id {new_id} with {top} already loaded")
    elif not indexed:
        behind.append("the inserted row got no search vector")

    if behind:
        return False, "; ".join(behind[:3]) + (
            f" (and {len(behind) - 3} more)" if len(behind) > 3 else "")
    return True, (f"{checked} identity counter(s) ahead of their rows; a real "
                  f"insert took id {new_id} and was rolled back")


AREAS: tuple[tuple[str, object], ...] = (
    ("minds", _minds),
    ("session picker", _session_picker),
    ("memory", _memory_shape),
    ("search index", _search_column),
    ("knowledge graph", _graph),
    ("board", _board),
    ("tools", _tools),
    ("proxy", _proxy),
    ("foreign keys", _keys),
    ("write path", _write_path),
)


async def run(dsn: str, snapshot: Path) -> Preflight:
    report = Preflight()
    conn = await asyncpg.connect(dsn)
    try:
        await load_data._register_vector(conn)
        for name, check in AREAS:
            try:
                ok, detail = await check(conn, snapshot)
            except (asyncpg.PostgresError, sqlite3.Error, OSError,
                    load_data.LoadRefused) as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            report.record(name, ok, detail)
    finally:
        await conn.close()
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get("HIVE_DATABASE_URL", ""))
    ap.add_argument("--snapshot", required=True,
                    help="the snapshot the load read")
    args = ap.parse_args(argv)
    if not args.dsn:
        print("REFUSED: no database named. Set HIVE_DATABASE_URL or pass "
              "--dsn.", file=sys.stderr)
        return 2
    report = asyncio.run(run(args.dsn, Path(args.snapshot)))
    width = max(len(a.area) for a in report.areas)
    for a in report.areas:
        print(f"{'ok  ' if a.ok else 'FAIL'} {a.area:<{width}}  {a.detail}")
    failed = [a.area for a in report.areas if not a.ok]
    print(f"\n{len(report.areas)} area(s), {len(failed)} failing"
          + (f": {', '.join(failed)}" if failed else ""))
    if not report.ok:
        return 1
    print("\nThe new database answers every question the old files answer, "
          "and the identity counters are safe.")
    print("Ready to switch over. After the cutover, run:\n"
          "  python -m unified_db.postflight")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
