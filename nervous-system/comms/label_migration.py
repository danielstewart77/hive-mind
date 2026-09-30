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
    forked: list[str] = field(default_factory=list)          # two successors: undecidable

    @property
    def total(self) -> int:
        return (
            len(self.moved) + len(self.kept)
            + len(self.skipped_named) + len(self.forked)
        )


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _descendant(
    session_id: str,
    children: dict[str, list[str]],
    status: dict[str, str],
) -> tuple[str | None, bool]:
    """The live conversation this one became, following rotation links forward.

    Returns ``(target, forked)``. ``target`` is ``None`` when there is nowhere
    live to move to; ``forked`` says whether that was because the lineage
    genuinely splits in two.

    The two are reported separately because they are different sentences. Most
    chains that yield no target are perfectly ordinary lineages three rotations
    deep that simply end on a conversation somebody closed — on the real data,
    every one of them — and calling that a fork sends the reader hunting a
    branch the database has never contained.

    A fork is declined rather than guessed: a confident wrong answer about which
    of two conversations inherited a name is worse than leaving it where a person
    can place it. The visited set is not decoration — `rotated_from` is written
    by code that cannot currently produce a cycle, and a migration that loops
    forever on a database which acquires one is not a failure anybody gets to
    read.
    """
    seen = {session_id}
    current = session_id
    while True:
        next_ids = children.get(current, [])
        if not next_ids:
            break
        if len(next_ids) > 1:
            return None, True
        nxt = next_ids[0]
        if nxt in seen:
            return None, False
        seen.add(nxt)
        current = nxt
    if current == session_id:
        return None, False
    if status.get(current) in LIVE_STATUSES:
        return current, False
    return None, False


def migrate_terminal_labels(
    labels_db_path: str,
    sessions_db_path: str,
    *,
    dry_run: bool = False,
) -> MigrationReport:
    """Move every name in `terminal_labels` onto its live session row.

    A name **moves**: the source row is deleted as it lands. That is what makes
    a second run a no-op rather than a replay, and it is the only version of
    idempotence that holds up. Declining to overwrite a target that already has
    a name looks equivalent and is not: a name the operator *cleared* after the
    first run is `NULL`, so the second run would put the old one back and
    requirement five would last exactly until somebody re-ran this to check it.

    Names are processed **newest first**, so when two names in one lineage
    compete for a single descendant the most recent one wins. Oldest-first is
    deterministic and wrong: a conversation named at its first tile, renamed
    three rotations later, would end up wearing the name the operator abandoned.
    """
    report = MigrationReport()
    labels = _open(labels_db_path)
    sessions = _open(sessions_db_path)
    try:
        # The columns are created here as well as by comms at startup, so this
        # can run *before* the gateway is restarted onto the new code. That
        # order matters: a restarted comms answers `/sessions/names` with
        # nothing until the move has happened, and the browser terminal replaces
        # its local cache with whatever the server says — so a comms-first
        # rollout wipes the last copy of every name outside this table during
        # the gap.
        for column in ("name TEXT", "color TEXT"):
            try:
                sessions.execute(f"ALTER TABLE sessions ADD COLUMN {column}")
                sessions.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists
        try:
            rows = labels.execute(
                "SELECT session_id, name, color FROM terminal_labels "
                "ORDER BY updated_at DESC, session_id"
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
            target, forked = _descendant(origin, children, status)
            if target is None:
                (report.forked if forked else report.kept).append(origin)
                target = origin
            elif target in named:
                report.skipped_named[origin] = target
                continue
            else:
                report.moved[origin] = target

            if not dry_run:
                sessions.execute(
                    "UPDATE sessions SET name = ?, color = ? WHERE id = ?",
                    (row["name"] or None, row["color"] or None, target),
                )
                # The name moved. Deleting the source is what stops a second
                # run replaying it over a name the operator has since changed
                # or cleared.
                labels.execute(
                    "DELETE FROM terminal_labels WHERE session_id = ?", (origin,)
                )
            named[target] = row["name"] or ""
        if not dry_run:
            sessions.commit()
            labels.commit()
    finally:
        labels.close()
        sessions.close()
    return report


def main(argv: list[str] | None = None) -> int:
    """Run the move from the command line, since nobody can import a module.

    Both databases are files on the host: the browser terminal's is the hive
    site's auth database, and the gateway's is comms' own. They live in
    different containers, so this runs on the host against the two paths rather
    than inside either one.

    `--dry-run` reports what would move without writing, which is the first
    thing to run: the answer names every conversation by id, and a name landing
    somewhere surprising is far cheaper to see before the write than after.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels_db", help="the browser terminal's database (terminal_labels)")
    parser.add_argument("sessions_db", help="hive-comms' sessions database")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    report = migrate_terminal_labels(
        args.labels_db, args.sessions_db, dry_run=args.dry_run
    )
    print(json.dumps({
        "dry_run": args.dry_run,
        "total": report.total,
        "moved": report.moved,
        "kept": report.kept,
        "skipped_named": report.skipped_named,
        "forked": report.forked,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
