"""One command: load the data, verify it, check it, and say whether to switch.

The load and the pre-switch check are separate commands with separate exit
codes, and neither knows the other ran — so "when both are green, the
operator is told it is done and asked whether to switch over" was a state no
code observed. This observes it.

    python -m unified_db.cutover --base /home/daniel/Storage

It takes a snapshot, loads it, verifies every table row for row, keeps the
snapshot, and runs the pre-switch check against that same copy. It stops at
the first thing that is not right rather than reporting the rest over it, and
the last line is the only one that matters: either the question, or what
stands in the way of asking it.

It does not switch anything. The cutover is its own story and its own hands.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

from . import load_data, preflight


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415
    import sqlite3  # noqa: PLC0415
    import sys  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="/home/daniel/Storage",
                    help="directory holding the Hive checkouts")
    ap.add_argument("--dsn", default=os.environ.get("HIVE_DATABASE_URL", ""))
    ap.add_argument("--work", default=None,
                    help="where to keep the snapshot (default: a new "
                         "temporary directory at 0700)")
    ap.add_argument("--snapshot", default=None,
                    help="re-run against a snapshot a previous run kept")
    args = ap.parse_args(argv)
    if not args.dsn:
        print("REFUSED: no database named. Set HIVE_DATABASE_URL or pass "
              "--dsn.", file=sys.stderr)
        return 2

    print("Loading and verifying...")
    try:
        report = asyncio.run(load_data.run(
            args.base, args.dsn,
            work=Path(args.work) if args.work else None,
            reuse=Path(args.snapshot) if args.snapshot else None,
            keep=True))
    except load_data.LoadRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError, asyncpg.PostgresError) as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    failing = [t for t in report.tables if not t.ok]
    print(f"{len(report.tables)} table(s), "
          f"{sum(t.source_rows for t in report.tables)} row(s), "
          f"{len(failing)} not matching")
    for t in failing:
        print(f"  FAIL {t.target.qualified}: {t.contents} {t.detail}")
    for note in report.notes:
        print(f"note: {note}")
    if failing:
        print("\nNot ready. The data in the database is not the data in the "
              "stores.")
        return 1

    print("\nChecking the loaded database...")
    checks = asyncio.run(preflight.run(args.dsn, report.snapshot_dir
                                       or Path(".")))
    width = max(len(a.area) for a in checks.areas)
    for a in checks.areas:
        print(f"{'ok  ' if a.ok else 'FAIL'} {a.area:<{width}}  {a.detail}")
    if not checks.ok:
        print("\nNot ready. The database holds the right rows and does not "
              "answer the right questions.")
        return 1

    if report.drift:
        print(f"\n{len(report.drift)} table(s) have moved on in the live "
              "stores since the snapshot was taken:")
        for line in report.drift:
            print(f"  {line}")
        print("\nThe load is good and it is already behind. This was a "
              "rehearsal. Stop the services and run this once more for the "
              "load that counts.")
        return 1

    print(f"\nDone. {sum(t.source_rows for t in report.tables)} rows across "
          f"{len(report.tables)} tables, every table verified against its "
          "source, every question answered, every identity counter safe, and "
          "nothing has been written to the stores since the snapshot.")
    print(f"The snapshot this was verified against is at "
          f"{report.snapshot_dir}.")
    print("\nReady to switch over. Shall I?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
