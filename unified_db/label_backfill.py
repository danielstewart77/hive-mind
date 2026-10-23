"""Move the terminal labels the earlier migration deliberately left behind.

`comms/label_migration.py` moved conversation names forward onto the live
session that continued the one each name was given to. Where a name's
rotation lineage ended in a *closed* session it moved nothing and said so:
carrying a name onto an equally unreachable row achieves nothing, and
discarding it is worse. Those names are still in the console's
`terminal_labels` table, and that table is their only copy.

So the table is not a dead leftover and is not dropped. Names and colours are
columns on the session row, and the rows those labels belong to do exist —
they are merely closed. This decides, per label, which session row it belongs
on, so the loader can write it there and names end up in the single place
names live.

**This is not run against the live `sessions.db`.** That file is in `delete`
journal mode rather than WAL, so a reader blocks a writer, and comms holds a
read-write lock on it for the life of its container — a write from here waits
out its busy timeout and fails, which is exactly what happened the first time
it was tried. The placement decision therefore travels with the data
migration and is applied to `core.sessions` in Postgres. The SQLite-to-SQLite
path remains because it is how the decision is tested and how it can be
rehearsed against a copy of the tree.

Two rules make the decision safe. A target row that already carries a name or
a colour is somebody's current label, and rather than decide whose is right
the whole run places nothing. And a label whose session row is absent
entirely has nowhere to go: it is reported unplaced, placed nowhere, and
`can_drop` is false — so the table outlives any run that could not account
for every label in it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class BackfillReport:
    """What the run did, per label, in terms a person can check."""

    #: session id -> (name, colour) actually written.
    placed: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: labels whose session row does not exist in the sessions store.
    unplaced: list[str] = field(default_factory=list)
    #: session id -> what the target row already held, which stopped the run.
    conflicts: dict[str, str] = field(default_factory=dict)

    @property
    def can_drop(self) -> bool:
        """Whether `terminal_labels` may now be dropped.

        Only when every label it holds was accounted for. An unplaced label
        or a conflict means the table is still the only home for something.
        """
        return not self.unplaced and not self.conflicts

    @property
    def total(self) -> int:
        return len(self.placed) + len(self.unplaced) + len(self.conflicts)


#: How long a write waits for a concurrent holder before giving up. These
#: stores are live — comms holds `sessions.db` open for the life of the
#: container — so a connection with SQLite's default of no wait fails on the
#: first byte anyone else is writing. Which is the whole reason this database
#: is moving to Postgres, but the move itself has to run against the live
#: file first.
BUSY_TIMEOUT_MS = 15_000


def _open(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return con


def backfill(labels_db: str, sessions_db: str, *, apply: bool = True) -> BackfillReport:
    """Move every `terminal_labels` row onto its own session row.

    `apply=False` reports what would happen and writes nothing, which is how
    the run is inspected before it is trusted.
    """
    report = BackfillReport()
    labels = _open(labels_db)
    sessions = _open(sessions_db)
    try:
        rows = labels.execute(
            "SELECT session_id, name, color FROM terminal_labels"
        ).fetchall()

        pending: list[tuple[str, str, str]] = []
        for row in rows:
            sid = row["session_id"]
            target = sessions.execute(
                "SELECT name, color FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if target is None:
                report.unplaced.append(sid)
                continue
            held_name = (target["name"] or "").strip()
            held_color = (target["color"] or "").strip()
            if held_name or held_color:
                report.conflicts[sid] = (
                    f"session row already holds name={held_name!r} "
                    f"color={held_color!r}"
                )
                continue
            pending.append((sid, row["name"] or "", row["color"] or ""))

        # Every conflict stops every write. A partial run would leave the
        # operator holding a table they cannot drop and rows they cannot
        # reason about, which is worse than a run that did nothing.
        if report.conflicts:
            return report

        for sid, name, color in pending:
            if apply:
                sessions.execute(
                    "UPDATE sessions SET name = ?, color = ? WHERE id = ?",
                    (name, color, sid),
                )
            report.placed[sid] = (name, color)
        if apply:
            sessions.commit()
        return report
    finally:
        labels.close()
        sessions.close()


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Move kept terminal labels onto their session rows.")
    ap.add_argument("--labels-db", default="/home/daniel/Storage/Dev/hive/data/hive.db")
    ap.add_argument("--sessions-db",
                    default="/home/daniel/Storage/Dev/hive_mind/nervous-system/data/sessions.db")
    ap.add_argument("--apply", action="store_true", help="write; otherwise report only")
    args = ap.parse_args()

    r = backfill(args.labels_db, args.sessions_db, apply=args.apply)
    print(f"labels: {r.total}  placed: {len(r.placed)}  "
          f"unplaced: {len(r.unplaced)}  conflicts: {len(r.conflicts)}")
    for sid, (n, c) in r.placed.items():
        print(f"  placed  {sid}  name={n!r} color={c!r}")
    for sid in r.unplaced:
        print(f"  UNPLACED {sid}  (no such session row)")
    for sid, why in r.conflicts.items():
        print(f"  CONFLICT {sid}  {why}")
    print(f"terminal_labels may be dropped: {r.can_drop}")
    if not args.apply:
        print("(report only — pass --apply to write)")
    sys.exit(0 if r.can_drop or not args.apply else 1)
