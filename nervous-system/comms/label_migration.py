"""The one-time move of conversation names onto the session row that owns them.

Names were kept in a `terminal_labels` table in the browser terminal's own
database — a different file, in a different container, keyed to `sessions.id`
and with no knowledge of a session's lifecycle. A chat rotation retires the
session row and mints a new id, so the name stayed behind on a row every
picker hides, and the conversation carried on under a new id displaying the
first hundred characters of its own first message. That is why a name given
weeks ago had to be looked up in a database to be found at all.

This module moves each name forward to the live conversation that continued
the one it was given to, and the walk is the whole point: a name three
rotations back is the normal case, not the exception.

It is a function taking two paths rather than a `sqlite3` one-liner because
the forward walk has decisions in it — an already-named descendant, a chain
that ends closed, a name whose lineage forks — and a decision that cannot be
tested is a decision nobody checked.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# A session whose row is in this state is still reachable by a surface, so it
# is somewhere a name can usefully land. Anything else is history: the name
# stays on the row it was given to rather than being moved onto a conversation
# that is equally unreachable, or thrown away.
LIVE_STATUSES = ("running", "idle", "suspended")


@dataclass
class MigrationReport:
    """What the run did, per name, in terms a person can check.

    A migration that prints a total tells you it ran. These lists tell you
    whether it was right — which of your names moved and where to, and which
    ones it deliberately refused to touch.
    """

    moved: dict[str, str] = field(default_factory=dict)      # old session id -> new
    kept: list[str] = field(default_factory=list)            # no live descendant
    skipped_named: dict[str, str] = field(default_factory=dict)  # target already named
    ambiguous: list[str] = field(default_factory=list)       # lineage forks

    @property
    def total(self) -> int:
        return (
            len(self.moved) + len(self.kept)
            + len(self.skipped_named) + len(self.ambiguous)
        )


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _descendant(
    session_id: str,
    children: dict[str, list[str]],
    status: dict[str, str],
) -> str | None:
    """The live conversation this one became, following rotation links forward.

    Returns ``None`` when the chain forks, because a confident wrong answer
    about which of two conversations inherited a name is worse than declining
    to move it: the name is still in the report, and still on its original
    row, for a person to place by hand.

    A chain whose every descendant is closed also yields ``None`` — there is
    nowhere live to move to. The visited set is not decoration: `rotated_from`
    is written by code that cannot currently produce a cycle, and a migration
    that loops forever on a database that acquires one is not a failure anybody
    gets to read.
    """
    seen = {session_id}
    current = session_id
    while True:
        next_ids = children.get(current, [])
        if not next_ids:
            break
        if len(next_ids) > 1:
            return None
        nxt = next_ids[0]
        if nxt in seen:
            return None
        seen.add(nxt)
        current = nxt
    if current == session_id:
        return None
    return current if status.get(current) in LIVE_STATUSES else None


def migrate_terminal_labels(
    labels_db_path: str,
    sessions_db_path: str,
    *,
    dry_run: bool = False,
) -> MigrationReport:
    """Move every name in `terminal_labels` onto its live session row.

    Safe to run twice, and that is a property of the rule rather than of a
    marker somewhere: a target that already carries a name is never written
    over. So the second run finds the first run's own work and declines — and
    so does a run that happens after the operator has renamed the successor by
    hand, which is the case a completion flag would have got wrong the first
    time somebody re-ran this to check it.

    Names are processed oldest first so that when two names in one lineage
    compete for a single descendant, the outcome does not depend on the order
    sqlite felt like returning rows in.
    """
    report = MigrationReport()
    labels = _open(labels_db_path)
    sessions = _open(sessions_db_path)
    try:
        try:
            rows = labels.execute(
                "SELECT session_id, name, color FROM terminal_labels "
                "ORDER BY updated_at, session_id"
            ).fetchall()
        except sqlite3.OperationalError:
            # No table means the move already happened and the old store was
            # dropped. Nothing to do is a clean outcome, not an error.
            return report

        session_rows = sessions.execute(
            "SELECT id, rotated_from, status, name FROM sessions"
        ).fetchall()
        children: dict[str, list[str]] = {}
        status: dict[str, str] = {}
        named: dict[str, str] = {}
        for row in session_rows:
            status[row["id"]] = row["status"]
            if row["name"]:
                named[row["id"]] = row["name"]
            if row["rotated_from"]:
                children.setdefault(row["rotated_from"], []).append(row["id"])

        for row in rows:
            origin = row["session_id"]
            if origin not in status:
                # A name for a session that no longer exists at all. Nothing to
                # attach it to, and inventing a row for it would put a
                # conversation in every listing that has no transcript behind
                # it.
                continue
            target = _descendant(origin, children, status)
            if target is None:
                if origin in children:
                    report.ambiguous.append(origin)
                else:
                    report.kept.append(origin)
                target = origin
            elif target in named:
                report.skipped_named[origin] = target
                continue
            else:
                report.moved[origin] = target

            if origin in named and target == origin:
                # Already carried across by an earlier run.
                report.kept = [k for k in report.kept if k != origin]
                report.skipped_named[origin] = origin
                continue
            if not dry_run:
                sessions.execute(
                    "UPDATE sessions SET name = ?, color = ? WHERE id = ?",
                    (row["name"] or None, row["color"] or None, target),
                )
            named[target] = row["name"] or ""
        if not dry_run:
            sessions.commit()
    finally:
        labels.close()
        sessions.close()
    return report
