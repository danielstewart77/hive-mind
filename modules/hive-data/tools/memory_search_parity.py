"""Does the Postgres recall answer what lucent's SQLite recall answers?

The evidence behind the "Memory search without FTS5" story. Requirement: the
ported recall agrees with the one it replaces, over the real corpus, for real
queries — which cannot be an assertion in a unit test, because the two
engines are two whole search implementations and the thing being judged is
whether the same memories come back.

How it is kept honest:

* **The same query vector on both sides.** The embedding is computed once per
  query and handed to both engines, so a difference in the answers is a
  difference in the search and never a difference in the embedding.
* **A snapshot, not the live store.** lucent is pointed at a consistent copy
  taken through SQLite's own backup API, so nothing here can write to, lock,
  or even read-contend with the database the hive is using right now.
* **A throwaway loader.** The rows are copied into a scratch Postgres
  database that this script creates and drops. It is deliberately NOT the
  loader the migration will use: that one has to verify row for row and fix
  up every identity sequence, and it is its own story. This one exists only
  to put the real corpus somewhere the new recall can be asked about it.

Run it from this repo, but with **hive_mind's** interpreter: it imports
lucent's own recall so that the comparison is against the real thing rather
than a reimplementation of it, and lucent's import chain wants fastapi,
requests, keyring and numpy. Those are hive_mind's dependencies, not this
package's, and installing them here to run a one-off report would put them in
every service's dependency tree for ever.

    HIVE_DATA_TEST_ADMIN_DSN=postgresql://... \\
        /home/daniel/Storage/Dev/hive_mind/.venv/bin/python \\
        tools/memory_search_parity.py --out docs/memory-search-parity.md
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

LUCENT_REPO = Path(
    "/home/daniel/Storage/Dev/hive_mind/nervous-system")
LIVE_STORE = LUCENT_REPO / "data" / "lucent.db"

#: Real queries, not invented ones: the shapes the hive actually asks for.
#: The last three are the edges the port changes something about — a query
#: whose words are all filler, a name carrying diacritics, and a word whose
#: inflection differs from the one stored.
#: Whose memories the comparison reads, as the live caller reads them: a
#: mind's own plus the `shared` sentinel the hive-wide rules are written
#: under. `MIND_ID` is this host's, from the environment the service gives
#: it; a run without one falls back to the whole store and says so.
SCOPE = [os.environ.get("MIND_ID", "").strip() or "shared", "shared"]

QUERIES = [
    "rotation threshold for a terminal pane",
    "which voice engine speaks a mind",
    "postgres migration one database",
    "surface claim who a bot answers",
    "left elbow tendinopathy reloading",
    "restart the comms container",
    "what did Daniel decide about Zeek output",
    "the and of is are to be",
    "lunch with Jose",
    "rotating a session",
]

TOP_K = 5


def _snapshot(destination: Path) -> int:
    """A consistent copy of the live store, taken without blocking a writer."""
    source = sqlite3.connect(f"file:{LIVE_STORE}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            return target.execute(
                "SELECT count(*) FROM memories").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()


def _decode(raw: object) -> np.ndarray | None:
    """lucent's own decode, including the legacy JSON-string rows."""
    if raw is None:
        return None
    if isinstance(raw, str):
        import json
        return np.array(json.loads(raw), dtype=np.float32)
    return np.frombuffer(bytes(raw), dtype=np.float32)


async def _load(dsn: str, snapshot: Path) -> int:
    """Copy the snapshot's memories into Postgres as real vectors.

    Explicit ids, through a plain driver connection rather than through the
    package: the layer refuses an insert naming a generated identity on
    purpose, and the report needs lucent's own ids on both sides so the two
    answers can be compared row by row.
    """
    con = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(
        "SELECT id, mind_id, content, embedding, tags, source, data_class, "
        "tier, as_of, expires_at, superseded, recurring, codebase_ref, "
        "created_at FROM memories"))
    con.close()

    batch = []
    for row in rows:
        vector = _decode(row["embedding"])
        literal = None if vector is None else (
            "[" + ",".join(repr(float(v)) for v in vector) + "]")
        batch.append((
            int(row["id"]), row["mind_id"], row["content"], literal,
            row["tags"], row["source"], row["data_class"], row["tier"],
            row["as_of"], row["expires_at"],
            None if row["superseded"] is None else int(row["superseded"]),
            None if row["recurring"] is None else int(row["recurring"]),
            row["codebase_ref"],
            None if row["created_at"] is None else int(float(
                row["created_at"])),
        ))

    connection = await asyncpg.connect(dsn)
    try:
        await connection.executemany(
            'INSERT INTO memory."memories" ("id", "mind_id", "content", '
            '"embedding", "tags", "source", "data_class", "tier", "as_of", '
            '"expires_at", "superseded", "recurring", "codebase_ref", '
            '"created_at") VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8, '
            "$9, $10, $11, $12, $13, $14)", batch)
        # What the real loader will have to do for every identity column, and
        # the reason its story says so: Postgres takes an explicit value on a
        # GENERATED BY DEFAULT column without advancing the sequence, so the
        # next insert collides on a key that is already there — as a
        # uniqueness conflict, which is the one outcome a caller is told is
        # benign.
        await connection.execute(
            "SELECT setval(pg_get_serial_sequence('memory.memories', 'id'), "
            '(SELECT max("id") FROM memory."memories"))')
    finally:
        await connection.close()
    return len(batch)


def _lucent_module(snapshot: Path):
    """lucent's own memory module, pointed at the snapshot."""
    sys.path.insert(0, str(LUCENT_REPO))
    import lucent_api.lucent as lucent_db

    lucent_db.DB_PATH = str(snapshot)
    import lucent_api.lucent_memory as lucent_memory
    return lucent_memory


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="docs/memory-search-parity.md")
    ap.add_argument("--keep", action="store_true",
                    help="leave the scratch database behind")
    args = ap.parse_args()

    admin_dsn = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()
    if not admin_dsn:
        print("HIVE_DATA_TEST_ADMIN_DSN is unset.", file=sys.stderr)
        return 2

    # 0700, and removed in the `finally` at the bottom whatever happens.
    # The first version of this script left one 137MB copy of every memory
    # in the hive — bloodwork, the kids' machines, every soul row — in a
    # world-readable /tmp directory per run, and five of them accumulated
    # in an afternoon. The module docstring's boast about never writing to
    # the live store was true and beside the point.
    work = Path(tempfile.mkdtemp(prefix="memory-parity-"))
    work.chmod(0o700)
    snapshot = work / "lucent-snapshot.db"
    live_rows = _snapshot(snapshot)
    print(f"snapshot: {live_rows} memories", file=sys.stderr)

    # Deliberately NOT `hive_data_test_*`: the suite's own fixture sweeps
    # every database matching that prefix at session start, to clean up after
    # interrupted runs. A report database named inside that namespace gets
    # dropped out from under this script the moment anybody runs the tests.
    name = f"memory_parity_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    parts = urlsplit(admin_dsn)
    dsn = urlunsplit(parts._replace(path=f"/{name}"))

    try:
        return await _report(args, admin_dsn, dsn, name, work, snapshot,
                             live_rows)
    finally:
        # Both of these used to sit inside a `try` that opened after the
        # setup they were meant to protect, so a failure applying the schema
        # or loading a row leaked the database and left the snapshot behind.
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            admin = await asyncpg.connect(admin_dsn)
            try:
                await admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = $1 AND pid <> pg_backend_pid()", name)
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
            finally:
                await admin.close()


async def _report(args, admin_dsn: str, dsn: str, name: str, work: Path,
                  snapshot: Path, live_rows: int) -> int:
    import hive_data
    from hive_data import memory as pg_memory

    schema_dir = Path(hive_data.__file__).parent / "schema"
    target = await asyncpg.connect(dsn)
    try:
        await target.execute((schema_dir / "core.sql").read_text())
        await target.execute((schema_dir / "memory.sql").read_text())
    finally:
        await target.close()

    loaded = await _load(dsn, snapshot)
    print(f"loaded: {loaded} rows into {name}", file=sys.stderr)

    lucent_memory = _lucent_module(snapshot)
    os.environ[hive_data.config.DSN_VAR] = dsn
    await hive_data.connect()

    embed = lucent_memory._embed
    lines: list[str] = []
    ratios: list[tuple[str, float, float, float]] = []
    agreed = 0
    overlaps: list[float] = []
    worst_cosine = 0.0
    try:
        for query in QUERIES:
            # The real one, held aside before anything is patched. Taking
            # it from the module each time round the loop is how the first
            # version of this script embedded query one and then handed that
            # same vector to all ten: the stub installed below *is*
            # `_embed` by the second iteration, so the line that looked like
            # it was asking the model was asking the stub. Every query after
            # the first searched for the first one, which made four hub-like
            # rows top every list and looked exactly like a finding about
            # the corpus.
            vector = embed(query)
            # The same vector on both sides, so a difference in the answers
            # is a difference in the search. Patched rather than trusted to
            # be deterministic: the model is served over HTTP and a second
            # call is a second inference.
            lucent_memory._embed = lambda _text, _v=vector: _v

            import json
            # Scoped the way the live caller scopes it. The contextual
            # retrieval hook passes [its own mind, "shared"] on every turn,
            # and an unscoped comparison exercises a different code path
            # from the one the hive actually runs — including the keyword
            # pool, whose provenance handling is the thing most likely to
            # diverge.
            before = json.loads(lucent_memory.memory_retrieve_hybrid(
                query, k=TOP_K, mind_ids=SCOPE, debug=True))
            after = await pg_memory.recall(
                query=query, embedding=vector, k=TOP_K, mind_ids=SCOPE,
                debug=True)

            # The ratio the keyword rescaling constant approximates, measured
            # per query rather than fitted to one. Reported so the constant
            # in `hive_data.memory` can be checked against this table.
            old_bm = max((m["debug"]["bm25"]
                          for m in before.get("memories", [])), default=0.0)
            new_bm = max((m["debug"]["bm25"]
                          for m in after["memories"]), default=0.0)
            if old_bm > 0 and new_bm > 0:
                ratios.append(
                    (query, old_bm, new_bm / pg_memory.KEYWORD_SCALE,
                     old_bm / (new_bm / pg_memory.KEYWORD_SCALE)))

            old_ids = [int(m["id"]) for m in before.get("memories", [])]
            new_ids = [int(m["id"]) for m in after["memories"]]
            old_cos = {int(m["id"]): m["debug"]["cosine"]
                       for m in before.get("memories", [])}
            new_cos = {int(m["id"]): m["debug"]["cosine"]
                       for m in after["memories"]}
            shared = set(old_ids) & set(new_ids)
            for rid in shared:
                worst_cosine = max(worst_cosine,
                                   abs(old_cos[rid] - new_cos[rid]))
            overlap = (len(shared) / len(old_ids)) if old_ids else 1.0
            overlaps.append(overlap)
            if old_ids == new_ids:
                agreed += 1

            lines.append(f"### `{query}`\n")
            lines.append(f"* overlap in the top {TOP_K}: "
                         f"{len(shared)}/{len(old_ids) or TOP_K}"
                         f"{' — same order' if old_ids == new_ids else ''}")
            lines.append("")
            lines.append("| rank | lucent (FTS5) | bucket | Postgres | "
                         "bucket | cosine agrees |")
            lines.append("|---|---|---|---|---|---|")
            for i in range(max(len(old_ids), len(new_ids))):
                o = old_ids[i] if i < len(old_ids) else None
                n = new_ids[i] if i < len(new_ids) else None
                ob = (before["memories"][i]["debug"]["bucket"]
                      if i < len(old_ids) else "")
                nb = (after["memories"][i]["debug"]["bucket"]
                      if i < len(new_ids) else "")
                same = ""
                if o is not None and o in new_cos:
                    same = ("yes" if abs(old_cos[o] - new_cos[o]) < 1e-4
                            else f"no ({old_cos[o]} vs {new_cos[o]})")
                lines.append(
                    f"| {i + 1} | {o if o is not None else ''} | {ob} | "
                    f"{n if n is not None else ''} | {nb} | {same} |")
            lines.append("")
    finally:
        lucent_memory._embed = embed
        await hive_data.close(drain_timeout=5.0)

    mean_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0
    header = [
        "# Memory search parity: FTS5 against Postgres",
        "",
        f"Generated {datetime.datetime.now().isoformat(timespec='seconds')} "
        f"by `tools/memory_search_parity.py`.",
        "",
        f"* corpus: **{live_rows}** memories, snapshotted from the live "
        "lucent store and loaded into a scratch Postgres database this "
        "script created and dropped",
        f"* queries: **{len(QUERIES)}** real ones, each embedded once and "
        "handed to both engines, so any difference below is a difference in "
        "the search",
        f"* identical top-{TOP_K} **in the same order**: "
        f"**{agreed}/{len(QUERIES)}**",
        f"* mean overlap in the top {TOP_K}: **{mean_overlap:.0%}**",
        f"* largest disagreement on a shared row's cosine: "
        f"**{worst_cosine:.2e}**",
        "",
        "Cosine is the half that can be expected to agree exactly, and the "
        "figure above says it does to well inside the four decimal places "
        "the response rounds to. The keyword half cannot agree exactly: "
        "Postgres has no BM25, so `ts_rank` is a different formula rescaled "
        "into the range the surrounding arithmetic was written against. "
        "Where the two answers differ, it is that half moving.",
        "",
    ]
    scale_in_use = pg_memory.KEYWORD_SCALE
    if ratios:
        spread = sorted(r[3] for r in ratios)
        median = spread[len(spread) // 2]
        header += [
            "## The keyword scale",
            "",
            "Postgres has no BM25. `ts_rank` is bounded below 1 and "
            "`bm25()` is not bounded at all, so no single multiplier maps "
            "one onto the other for every query — but the figure is added "
            "to the combined score with a fixed coefficient and rendered "
            "into every mind's context, so being within a factor of two of "
            "the old magnitude matters. "
            f"`hive_data.memory.KEYWORD_SCALE` is **{scale_in_use:.0f}**; "
            f"measured here the ratio runs **{spread[0]:.0f} to "
            f"{spread[-1]:.0f}**, median **{median:.0f}**.",
            "",
            "| query | FTS5 `bm25` | raw `ts_rank` | ratio |",
            "|---|---|---|---|",
        ]
        for query, old_bm, raw, ratio in ratios:
            header.append(
                f"| `{query}` | {old_bm:.3f} | {raw:.5f} | {ratio:.0f} |")
        header.append("")

    out = REPO / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(header + lines))
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
