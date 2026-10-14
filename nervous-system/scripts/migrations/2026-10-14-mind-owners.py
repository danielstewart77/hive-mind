"""One-shot: stamp an owner on every mind registered before ownership existed.

Sloan owns Arnold, Aiden owns Zack (registered as ``zach``), and every other
mind — everything that exists today — is Daniel's. Keyed by ``mind_id``, not
name, because ownership is: a renamed mind keeps its owner.

Only rows whose owner is still unset are touched, so a mind that has since
registered naming its owner, or one an admin has already moved, is left
alone and the script is safe to run twice.

Prerequisites:
  - hive-comms has started at least once on the code that adds the
    ``minds.owner`` column (its startup migration adds it).

Dry run by default; prints what it would stamp. ``--apply`` writes.

    python3 scripts/migrations/2026-10-14-mind-owners.py data/broker.db
    python3 scripts/migrations/2026-10-14-mind-owners.py data/broker.db --apply

Run from /home/USER/Storage/Dev/hive_mind/nervous-system/.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

DEFAULT_OWNER = "daniel"

# mind_id -> owner, for the minds that are not Daniel's.
OWNERS_BY_MIND_ID = {
    "5437b578-e958-46a6-a73e-78918f70d01a": "sloan",  # arnold
    "82cd16ef-f33e-4a90-9fbc-d24427b96680": "aiden",  # zach (Zack)
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", help="path to broker.db")
    parser.add_argument("--apply", action="store_true", help="write the owners")
    args = parser.parse_args()

    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    columns = {r[1] for r in con.execute("PRAGMA table_info(minds)")}
    if "owner" not in columns:
        sys.exit("minds.owner does not exist yet: start hive-comms on the new code first")

    rows = con.execute(
        "SELECT mind_id, name, owner FROM minds ORDER BY name"
    ).fetchall()
    plan = []
    for row in rows:
        if row["owner"]:
            print(f"keep   {row['name']:<12} {row['mind_id']}  owner={row['owner']}")
            continue
        owner = OWNERS_BY_MIND_ID.get(row["mind_id"], DEFAULT_OWNER)
        plan.append((owner, row["mind_id"]))
        print(f"stamp  {row['name']:<12} {row['mind_id']}  owner={owner}")

    if not args.apply:
        print(f"dry run: {len(plan)} row(s) would be stamped; pass --apply to write")
        return 0

    with con:
        for owner, mind_id in plan:
            con.execute(
                "UPDATE minds SET owner = ? WHERE mind_id = ? AND owner IS NULL",
                (owner, mind_id),
            )
    unset = con.execute("SELECT COUNT(*) FROM minds WHERE owner IS NULL").fetchone()[0]
    print(f"stamped {len(plan)} row(s); rows still without an owner: {unset}")
    return 0 if unset == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
