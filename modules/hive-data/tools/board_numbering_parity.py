"""Does the ported numbering answer what the console's own numbering answers?

The evidence behind requirement two of "The access layer learns the board
schema": *ordering and the parent-child hierarchy answer exactly as they do
today, including the derived backlog numbers*. That cannot be a unit test.
The claim is about **this** board - hundreds of rows whose numbers appear in
conversations, commit messages and the lucent graph - and a suite that needed
one machine's rows to be present would fail on every other machine.

So it is a report, run when the numbering changes, the way the memory search
parity report is.

How it is kept honest:

* **Both sides are the real implementation.** `kanban_data.backlog_numbers`
  is imported from the console's own checkout and called, not reimplemented
  here — through the console's own store, opened on the file being compared,
  since that module reads whichever store it is pointed at. `hive_data.board.backlog_numbers` is called through the layer.
* **Both sides hold the same rows.** The console's board is copied into a
  scratch Postgres database this script creates and drops, so a difference in
  the answers is a difference in the numbering and never a difference in the
  rows. Comparing against the hive's live `board` schema instead does not
  work: the console still writes to SQLite until its own cutover story, so
  that copy is behind by however many items have been filed since it was
  loaded, and every number below a missing row shifts for a reason that has
  nothing to do with this code.
* **Nothing is written to either live store.** The SQLite file is opened by
  the console's own read path; the scratch database is this script's own and
  is dropped on the way out.
* **Every row is compared, not a sample.** A number that moved is named with
  the id, the title, and both answers.

    HIVE_DATA_TEST_ADMIN_DSN=postgresql://... \\
        PYTHONPATH=. .venv/bin/python tools/board_numbering_parity.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import asyncpg

import hive_data
from hive_data import board
from hive_data.schema import COLUMNS

from tests._scratch import claimed_database

#: The console's checkout, whose `kanban_data` is the thing being compared
#: against. Named rather than discovered, and overridable, because importing
#: the wrong copy would make this report agree with itself.
DEFAULT_CONSOLE_SRC = Path("/home/daniel/Storage/Dev/hive/src")
DEFAULT_SQLITE = Path("/home/daniel/Storage/Dev/hive/data/hive.db")

_SCHEMA_DIR = Path(hive_data.__file__).parent / "schema"


async def console_numbers(console_src: Path,
                          db_path: Path) -> dict[str, str]:
    """The numbers the console computes today, from the console's own code.

    Through the console's own store, opened on the file being compared and
    with `HIVE_DATABASE_URL` cleared: the console's numbering is async now
    and reads whichever store that module is pointed at, so a report run on
    the machine that serves the console would otherwise read the numbers out
    of Postgres and compare them against themselves.
    """
    sys.path.insert(0, str(console_src))
    os.environ["HIVE_DB_PATH"] = str(db_path)
    os.environ[hive_data.config.DSN_VAR] = ""
    import kanban_data  # noqa: E402  - the path is set above on purpose
    import store as console_store  # noqa: E402  - as above

    await console_store.open(db_path)
    try:
        assert not console_store.on_postgres()
        return await kanban_data.backlog_numbers()
    finally:
        await console_store.close()


def sqlite_rows(db_path: Path) -> list[tuple]:
    """Every work item, in the column order the declaration gives.

    Read with `sqlite3` directly rather than through the console, because
    what is wanted here is the rows and not the records the console shapes
    them into.
    """
    columns = COLUMNS[board.ITEMS]
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return connection.execute(
            f"SELECT {', '.join(columns)} FROM work_items").fetchall()
    finally:
        connection.close()


async def layer_numbers(admin_dsn: str,
                        rows: list[tuple]) -> tuple[dict[str, str],
                                                    dict[str, str]]:
    """The numbers the layer computes over the same rows, and their titles."""
    columns = COLUMNS[board.ITEMS]
    async with claimed_database(admin_dsn, "hive_data_parity_") as dsn:
        target = await asyncpg.connect(dsn)
        try:
            await target.execute(
                (_SCHEMA_DIR / "core.sql").read_text())
            await target.execute(
                (_SCHEMA_DIR / "board.sql").read_text())
            # Copied with the key deferred, which it already is, so the rows
            # go in whatever order the console's table has them in rather
            # than needing a topological sort.
            await target.copy_records_to_table(
                "work_items", schema_name="board", columns=list(columns),
                records=rows)
        finally:
            await target.close()
        os.environ[hive_data.config.DSN_VAR] = dsn
        await hive_data.connect()
        try:
            loaded = await board.work_items()
        finally:
            await hive_data.close()
    return (board.numbers_for(loaded),
            {row["id"]: row["title"] for row in loaded})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console-src", type=Path,
                        default=DEFAULT_CONSOLE_SRC)
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE)
    args = parser.parse_args()

    admin_dsn = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()
    if not admin_dsn:
        print("HIVE_DATA_TEST_ADMIN_DSN is unset. It names a Postgres this "
              "report may create and drop a database on.", file=sys.stderr)
        return 2

    before = asyncio.run(console_numbers(args.console_src, args.sqlite))
    rows = sqlite_rows(args.sqlite)
    after, titles = asyncio.run(layer_numbers(admin_dsn, rows))

    only_sqlite = sorted(set(before) - set(after))
    only_postgres = sorted(set(after) - set(before))
    moved = sorted(item for item in set(before) & set(after)
                   if before[item] != after[item])

    print(f"rows on the console's board: {len(before)}")
    print(f"rows copied into Postgres:   {len(after)}")
    print(f"numbers that differ:         {len(moved)}")
    print(f"rows missing on one side:    "
          f"{len(only_sqlite) + len(only_postgres)}")
    for item in only_sqlite:
        print(f"  only in SQLite:   {item} {before[item]!r}")
    for item in only_postgres:
        print(f"  only in Postgres: {item} {after[item]!r} "
              f"{titles.get(item, '')!r}")
    for item in moved:
        print(f"  moved: {item} {before[item]} -> {after[item]} "
              f"{titles.get(item, '')!r}")

    return 0 if not (moved or only_sqlite or only_postgres) else 1


if __name__ == "__main__":
    raise SystemExit(main())
