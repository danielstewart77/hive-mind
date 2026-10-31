"""The named operations over the `board` schema.

The console's own store: the Kanban board every epic, feature and story in
the hive lives on, the design sessions whose three pages are read before any
code is written, the notices that carry a submission back to a mind, and two
small settings tables.

Three things here are not a transcription of the SQLite module this replaces.

**The order has a tie-break that exists.** Every read on the board was
`ORDER BY position, rowid`, and Postgres has no `rowid`. Ties break on `id`
instead. That decides nothing the old order decided: `position` is unique
within a parent on the loaded board - 639 rows, no two siblings sharing one -
so sibling order is settled by `position` alone, and the only ties are
between rows under *different* parents in a flat listing every consumer
regroups by parent anyway. What it buys is that the flat listing is the same
list twice in a row, which `ORDER BY "position"` on its own does not promise.

**The cascade is the database's.** The SQLite module walked the tree in
Python and deleted the whole frontier in one statement, because SQLite's
`foreign_keys` pragma is off by default and per connection, so a declared
cascade was decorative on any connection that forgot it - and the orphans it
left still rendered on the board while being unreachable from the only
delete button. Here the key is real and `DEFERRABLE INITIALLY DEFERRED`, so
`delete_subtree` is one recursive statement and the constraint is checked
once at commit, after the parent and its children have all gone.

**The deferred key is honoured rather than worked around.** A child may be
written before its parent inside one unit of work, which is what
`DEFERRABLE INITIALLY DEFERRED` is for and what a loader restoring a subtree
needs. The cost is that a `parent_id` naming nothing at all is refused at
*commit* rather than at the statement - a `MissingReference` out of the
`async with`, not out of the operation. Nothing here sets the constraints
back to `IMMEDIATE` to move that error earlier: doing so would also refuse
the legitimate out-of-order write, which is the whole reason the key is
deferred.

Every operation takes an optional `unit` and behaves as the `core` ones do:
given one it runs inside that caller's transaction, given none it runs on
its own connection and commits by itself.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from . import _sql
from .core import _execute, _execute_returning, _fetch, _fetch_one
from .errors import ValueRefused
from .schema import COLUMNS, qualified, select_list
from .unit import Unit

Row = dict[str, Any]

ITEMS = "board.work_items"
SESSIONS = "board.design_sessions"
NOTICES = "board.design_notices"
CAPABILITIES = "board.capability_node_settings"
EDGE_MINDS = "board.edge_mind_settings"
REQUESTERS = "board.operation_requesters"

#: The three levels of the board, as `kind` spells them.
KINDS = ("epic", "feature", "story")

#: The lowest position a row may be given. A row below it is one somebody
#: placed deliberately, which is what `first_position` returns.
MIN_POSITION = 1

#: The advisory-lock key every reordering write takes. One key for the whole
#: board rather than one per parent: a move changes the sibling order of the
#: parent it left as well as the one it joined, and `place_before` does not
#: know which parent it is about until it has read the row it is landing in
#: front of. Two locks taken in whichever order each caller discovered them
#: is a deadlock between two drags on one page.
BOARD_KEY = "hive:board"


async def hold_board(unit: Unit) -> None:
    """Serialize this unit against every other reordering write.

    Taken before anything else in the unit, which `Unit.hold` enforces. The
    SQLite module opened `BEGIN IMMEDIATE` on every mutation for this reason
    and the reason survives the port: each one reads the sibling list,
    decides a new numbering against it and writes the whole list back, so
    two of them interleaved is one tab's drag silently replaced by the
    other's, both reporting success, and - because each writes positions it
    computed against a list that has since changed - two siblings left
    holding one position.

    Postgres gives no equivalent of a file-wide write lock, and row locks
    are no help: the rows a reorder will renumber are not known until it has
    read them, so the second writer's `SELECT` does not block on the first
    writer's `UPDATE` of rows it is about to compute differently.
    """
    await unit.hold(BOARD_KEY)


# ------------------------------------------------------------------- reading

#: Every column of a work item, plus the `kind` of the row above it. The
#: parent's kind is what decides whether a story's parent is reported as its
#: feature or as its epic, and reading it in the same statement is what keeps
#: a listing from being one query per row.
_ITEM_SELECT = (
    ", ".join(f'item."{c}"' for c in COLUMNS[ITEMS])
    + ', parent."kind" AS "parent_kind"'
)

_ITEM_FROM = (
    f'FROM {qualified(ITEMS)} AS item '
    f'LEFT JOIN {qualified(ITEMS)} AS parent ON parent."id" = item."parent_id"'
)

#: The board's own order. `position` then `id`, for the reason the module
#: docstring gives.
_ITEM_ORDER = 'ORDER BY item."position", item."id"'


async def work_items(*, kind: str | None = None,
                     unit: Unit | None = None) -> list[Row]:
    """Every work item in board order, or every one of a single kind.

    One statement for the whole board rather than three. The console renders
    epics, features and stories from one payload and the numbering walks all
    of them, and three unguarded reads are three points in time - a write
    landing between them returns a story whose epic is not in the same
    payload, which the board draws as "Unassigned" and the backlog tree does
    not draw at all.
    """
    if kind is None:
        return await _fetch(
            unit, f"SELECT {_ITEM_SELECT} {_ITEM_FROM} {_ITEM_ORDER}")
    if kind not in KINDS:
        raise ValueRefused(
            f"{kind!r} is not a kind of work item. The board has "
            f"{', '.join(KINDS)} and nothing else, and a kind that matches no "
            "row comes back as an empty board rather than as a typo.")
    return await _fetch(
        unit,
        f'SELECT {_ITEM_SELECT} {_ITEM_FROM} WHERE item."kind" = $1 '
        f"{_ITEM_ORDER}", kind)


async def work_item(item_id: str, *, unit: Unit | None = None) -> Row | None:
    """One work item by id, or None when the board has no such row."""
    return await _fetch_one(
        unit, f'SELECT {_ITEM_SELECT} {_ITEM_FROM} WHERE item."id" = $1',
        item_id)


async def children(parent_id: str | None, *, kind: str | None = None,
                   unit: Unit | None = None) -> list[Row]:
    """The rows directly under a parent, in sibling order.

    `None` means the rows with no parent at all, and is spelled `IS NULL`
    rather than compared - `parent_id = NULL` is never true, so the natural
    reading of "the top level" would come back empty.
    """
    where = ('item."parent_id" IS NULL' if parent_id is None
             else 'item."parent_id" = $1')
    args: list[Any] = [] if parent_id is None else [parent_id]
    if kind is not None:
        if kind not in KINDS:
            raise ValueRefused(
                f"{kind!r} is not a kind of work item: "
                f"{', '.join(KINDS)}.")
        args.append(kind)
        where += f' AND item."kind" = ${len(args)}'
    return await _fetch(
        unit, f"SELECT {_ITEM_SELECT} {_ITEM_FROM} WHERE {where} "
        f"{_ITEM_ORDER}", *args)


async def descendants(item_id: str, *,
                      unit: Unit | None = None) -> list[Row]:
    """Every row beneath this one, however deep, in board order.

    Walked by the database rather than by the caller. The depth is two
    today, and a cascade that assumed two silently leaves rows behind the
    moment a third appears - and those are exactly the rows that cannot be
    deleted afterwards, because the only delete button is on the tree they
    have fallen out of.

    `UNION` rather than `UNION ALL`, so a row that is somehow its own
    ancestor is visited once instead of making this statement run for ever.
    """
    return await _fetch(
        unit,
        "WITH RECURSIVE below AS ("
        f'  SELECT "id" FROM {qualified(ITEMS)} WHERE "parent_id" = $1'
        "  UNION"
        f'  SELECT child."id" FROM {qualified(ITEMS)} AS child '
        '    JOIN below ON below."id" = child."parent_id"'
        ") "
        f'SELECT {_ITEM_SELECT} {_ITEM_FROM} '
        'WHERE item."id" IN (SELECT "id" FROM below) '
        f"{_ITEM_ORDER}", item_id)


async def next_position(*, unit: Unit | None = None) -> int:
    """A position after every row the board has ever held.

    `MAX(position) + 1` over the whole table, not a count of what is there.
    A created row has to sort after everything that exists *and* after
    everything that has been deleted, or deleting from the middle hands the
    next row a slot it has to share - which is how two siblings come to
    hold one position in the first place.
    """
    row = await _fetch_one(
        unit, f'SELECT COALESCE(MAX("position"), $1) + 1 AS "next" '
        f"FROM {qualified(ITEMS)}", MIN_POSITION - 1)
    assert row is not None  # an aggregate always answers
    return int(row["next"])


async def first_position(*, unit: Unit | None = None) -> int:
    """A position before every row there is, so a new top-level item sorts
    to the top."""
    row = await _fetch_one(
        unit, f'SELECT COALESCE(MIN("position"), $1) - 1 AS "first" '
        f"FROM {qualified(ITEMS)}", MIN_POSITION)
    assert row is not None
    return int(row["first"])


# ------------------------------------------------------ the derived numbering

#: The parent a row is numbered under, which is not always the parent it
#: stores. Carried over from the console's own numbering unchanged, including
#: the two cases below, because a port that renumbers the board is the one
#: failure this story was written to avoid - every story's number appears in
#: conversations, commit messages and the lucent graph.
#:
#: An **epic** is always a root, whatever `parent_id` holds. `change_type`
#: can give an epic a parent, and the record the board renders carries no
#: parent key for an epic at all.
#:
#: A **story under a story** is also a root: the record carries its parent
#: under `story_id`, and the numbering reads only `parent_id`, `feature_id`
#: and `epic_id`. Both of these are arguably wrong and neither is this
#: story's to change.
def numbering_parent(row: Row) -> str | None:
    """The id this row is numbered under, or None when it numbers as a root."""
    kind = row.get("kind")
    if kind == "feature":
        return row.get("parent_id")
    if kind == "story" and row.get("parent_kind") in ("feature", "epic"):
        return row.get("parent_id")
    return None


def numbers_for(rows: Sequence[Row]) -> dict[str, str]:
    """The hierarchy number of every row, from rows already in board order.

    Separate from the read so the console can number a board it already has
    without asking for it twice, and so the numbering can be tested against
    a shape built in a test rather than only against a query.

    A row whose numbering parent is not in `rows` numbers as nothing - an
    empty string rather than a missing key, because the number is rendered
    and a `KeyError` on a half-loaded board is a page that does not render
    at all.
    """
    by_parent: dict[str | None, list[Row]] = {}
    for row in rows:
        by_parent.setdefault(numbering_parent(row), []).append(row)

    numbers: dict[str, str] = {}

    def walk(parent_id: str | None, prefix: str) -> None:
        for index, row in enumerate(by_parent.get(parent_id, []), start=1):
            number = f"{prefix}.{index}" if prefix else str(index)
            numbers[row["id"]] = number
            walk(row["id"], number)

    walk(None, "")
    for row in rows:
        numbers.setdefault(row["id"], "")
    return numbers


async def backlog_numbers(*, unit: Unit | None = None) -> dict[str, str]:
    """Every row's hierarchy number, from one read of the whole board.

    One statement, so the numbers are all from the same moment. Numbered
    from two reads, a row created between them is a tree missing the parent
    its children hang off, and every number below that parent shifts.
    """
    return numbers_for(await work_items(unit=unit))


# ------------------------------------------------------------------- writing

#: What an update may set. The title, the prose pages and the status are the
#: operator's; `id` and `kind` are not, and `parent_id` and `position` move
#: together through `place` rather than one at a time - a parent changed
#: without a position is a row at whatever number it held under its old
#: parent, which is a duplicate under the new one.
_ITEM_FIELDS = frozenset({
    "title", "status", "description", "requirements", "acceptance_criteria",
    "qa_tests", "subtype",
})


async def create_work_item(item_id: str, kind: str, title: str,
                           position: int, *, parent_id: str | None = None,
                           status: str | None = None, subtype: str = "",
                           description: str = "", requirements: str = "",
                           acceptance_criteria: str = "", qa_tests: str = "",
                           unit: Unit | None = None) -> Row:
    """Write a work item at a position the caller chose.

    The position is an argument rather than read here, because a creation
    that lands at the top and one that lands at the bottom are two different
    positions and both are decided by the page. Read it with
    `next_position` or `first_position` inside the same unit as this write,
    or two creations race and take the same slot.
    """
    if kind not in KINDS:
        raise ValueRefused(
            f"{kind!r} is not a kind of work item: {', '.join(KINDS)}.")
    sql, args = _sql.insert(ITEMS, {
        "id": item_id, "kind": kind, "parent_id": parent_id, "title": title,
        "status": status, "position": position, "subtype": subtype,
        "description": description, "requirements": requirements,
        "acceptance_criteria": acceptance_criteria, "qa_tests": qa_tests,
    })
    return await _execute(unit, sql, *args)


async def update_work_item(item_id: str, fields: dict[str, Any], *,
                           unit: Unit | None = None) -> Row:
    """Set a work item's own fields, leaving its place on the board alone."""
    sql, args = _sql.update(ITEMS, _sql.passed(fields), {"id": item_id},
                            settable=_ITEM_FIELDS)
    return await _execute(unit, sql, *args)


async def place(item_id: str, parent_id: str | None, position: int, *,
                unit: Unit | None = None) -> Row:
    """Put a row under a parent at a position, in one statement.

    One statement for both, because they are one fact. A row whose parent
    moved and whose position did not is a row holding a number that was only
    unique under the parent it left.
    """
    if parent_id == item_id:
        raise ValueRefused(
            "a work item cannot be its own parent. The board walks down from "
            "its roots, so a row pointing at itself is a row that renders "
            "nowhere and a recursive read that never ends.")
    sql, args = _sql.update(
        ITEMS, {"parent_id": parent_id, "position": position}, {"id": item_id},
        settable=frozenset({"parent_id", "position"}))
    return await _execute(unit, sql, *args)


async def renumber(ordered_ids: Iterable[str], *,
                   start: int = MIN_POSITION,
                   unit: Unit | None = None) -> Row:
    """Give a sibling list consecutive positions, in one statement.

    One statement rather than one per row. A loop of updates inside a unit
    is the same transaction and so is equally atomic, but it is also one
    round trip per story - a long epic's reorder being tens of them - and
    the list has to be written whole or the board is briefly a board with
    two rows at one number.

    `VALUES` joined on the id, rather than `position = position + 1` style
    arithmetic, because the caller has already decided the whole order and
    arithmetic cannot express an arbitrary permutation.
    """
    ids = list(ordered_ids)
    if not ids:
        raise ValueRefused(
            "a renumber was given no ids. An empty sibling list is a parent "
            "with no children, which needs no write - and an UPDATE with an "
            "empty VALUES list is a syntax error, not a no-op.")
    if len(set(ids)) != len(ids):
        raise ValueRefused(
            "a renumber named the same work item twice. Two positions for "
            "one row is a sibling list that cannot be written, and the one "
            "that would land is whichever the planner reached last.")
    return await _execute(
        unit,
        f'UPDATE {qualified(ITEMS)} AS item SET "position" = wanted."position" '
        'FROM UNNEST($1::text[], $2::bigint[]) AS wanted("id", "position") '
        'WHERE item."id" = wanted."id"',
        ids, list(range(start, start + len(ids))))


async def delete_subtree(item_id: str, *,
                         unit: Unit | None = None) -> Row:
    """Delete a row and everything beneath it, in one statement.

    The row count is the whole subtree, which is what the page should report
    rather than the count it predicted before the delete: the browser's copy
    of the board is whatever it last loaded, and work added underneath since
    then goes too.

    One statement, so the self-referencing key is satisfied by the delete
    being complete rather than by the order inside it - which is also why
    nothing here disables or defers anything: the constraint is already
    deferred and is checked once, at commit, with the whole subtree gone.
    """
    return await _execute(
        unit,
        "WITH RECURSIVE below AS ("
        f'  SELECT "id" FROM {qualified(ITEMS)} WHERE "id" = $1'
        "  UNION"
        f'  SELECT child."id" FROM {qualified(ITEMS)} AS child '
        '    JOIN below ON below."id" = child."parent_id"'
        ") "
        f'DELETE FROM {qualified(ITEMS)} '
        'WHERE "id" IN (SELECT "id" FROM below)', item_id)


# ---------------------------------------------------------- design sessions

async def create_design_session(design_id: str, title: str, created_at: float,
                                updated_at: float, *,
                                conversation_id: str | None = None,
                                surface: str | None = None,
                                mind_id: str | None = None,
                                repos: str = "[]",
                                unit: Unit | None = None) -> Row:
    """Open a design session.

    The five JSON columns take the defaults the applied schema declares, so
    a session is created with five empty objects without this operation
    restating what they are.
    """
    sql, args = _sql.insert(SESSIONS, {
        "id": design_id, "title": title, "conversation_id": conversation_id,
        "surface": surface, "mind_id": mind_id, "repos": repos,
        "created_at": created_at, "updated_at": updated_at,
    })
    return await _execute(unit, sql, *args)


async def design_session(design_id: str, *,
                         unit: Unit | None = None) -> Row | None:
    """One design session by id, or None."""
    return await _fetch_one(
        unit, f"SELECT {select_list(SESSIONS)} FROM {qualified(SESSIONS)} "
        'WHERE "id" = $1', design_id)


async def design_sessions(*, unit: Unit | None = None) -> list[Row]:
    """Every design session, most recently written first.

    By `updated_at`, which only a write moves, so opening a session to read
    it leaves it where it was and the top of the list stays a record of what
    is in flight rather than of what was looked at last.
    """
    return await _fetch(
        unit, f"SELECT {select_list(SESSIONS)} FROM {qualified(SESSIONS)} "
        'ORDER BY "updated_at" DESC, "id" DESC')


async def attach_design_session(design_id: str, conversation_id: str | None,
                                updated_at: float, *,
                                surface: str | None = None,
                                mind_id: str | None = None,
                                unit: Unit | None = None) -> Row:
    """Point a design session at a conversation.

    `surface` and `mind_id` are kept when not given, which is what
    `COALESCE` is doing rather than a convenience: a mind re-claims a
    session on every turn that touches it and knows the conversation it is
    on, while the surface was decided when the page was opened.

    The revision is deliberately not bumped. A bump would invalidate
    whatever the operator was halfway through typing, on every turn.
    """
    return await _execute(
        unit,
        f'UPDATE {qualified(SESSIONS)} SET "conversation_id" = $1, '
        '"surface" = COALESCE($2, "surface"), '
        '"mind_id" = COALESCE($3, "mind_id"), "updated_at" = $4 '
        'WHERE "id" = $5',
        conversation_id, surface, mind_id, updated_at, design_id)


#: The three pages a design session carries, as the columns spell them. A
#: page name arrives from an HTTP path, and the column it names cannot be a
#: parameter - so it is checked against this and anything else is refused.
PAGES = ("requirements", "tests", "functions")


async def write_page(design_id: str, page: str, items: str, accepted: str,
                     stale: str, expected_revision: int, updated_at: float, *,
                     unit: Unit | None = None) -> Row:
    """Write one page, only if the session is still at the revision read.

    The revision is in the statement, not checked in Python beforehand. A
    read, a check and a write leave a window two browser tabs both pass,
    and the second one wins silently - which is the whole reason the column
    exists. Zero rows changed is the loser; the caller reads the revision
    back to say which.
    """
    if page not in PAGES:
        raise ValueRefused(
            f"{page!r} is not a page of a design session: "
            f"{', '.join(PAGES)}. The page names a column and a column name "
            "cannot travel as a parameter, so anything else is refused "
            "rather than written into the statement.")
    return await _execute(
        unit,
        f'UPDATE {qualified(SESSIONS)} SET "{page}" = $1, "accepted" = $2, '
        '"stale" = $3, "revision" = "revision" + 1, "updated_at" = $4 '
        'WHERE "id" = $5 AND "revision" = $6',
        items, accepted, stale, updated_at, design_id, expected_revision)


async def delete_design_session(design_id: str, *,
                                unit: Unit | None = None) -> Row:
    """Remove a design session and the notices queued against it.

    Both in one statement, and the count reported is the session's - which
    is the one that tells a caller whether the id was ever there.

    `design_notices.design_id` carries no foreign key, so nothing in the
    database removes the notices when the session goes. Left behind they are
    undelivered rows the delivery path keeps picking up and joining to a
    session that is gone, which drops them from its own listing - so they
    are never delivered and never cleared either.
    """
    return await _execute(
        unit,
        f'WITH "queued" AS (DELETE FROM {qualified(NOTICES)} '
        '  WHERE "design_id" = $1) '
        f'DELETE FROM {qualified(SESSIONS)} WHERE "id" = $1', design_id)


# ------------------------------------------------------------------ notices

async def queue_notice(design_id: str, kind: str, page: str,
                       created_at: float, *, body: str = "",
                       unit: Unit | None = None) -> Row:
    """Queue a notice telling a mind that a page was submitted."""
    sql, args = _sql.insert(NOTICES, {
        "design_id": design_id, "kind": kind, "page": page, "body": body,
        "created_at": created_at,
    }, returning=("id",))
    return await _execute_returning(unit, sql, *args)


async def pending_notices(design_id: str | None = None, *,
                          unit: Unit | None = None) -> list[Row]:
    """Undelivered notices, oldest first, with the conversation to reach.

    Joined to the session rather than read in a second statement: the
    delivery path needs the conversation, the surface and the mind to send
    anything at all, and a notice whose session was deleted between the two
    reads is a send with nowhere to go.
    """
    columns = ", ".join(f'notice."{c}"' for c in COLUMNS[NOTICES])
    sql = (f'SELECT {columns}, session."title", session."conversation_id", '
           'session."surface", session."mind_id" '
           f'FROM {qualified(NOTICES)} AS notice '
           f'JOIN {qualified(SESSIONS)} AS session '
           '  ON session."id" = notice."design_id" '
           'WHERE notice."delivered_at" IS NULL')
    args: list[Any] = []
    if design_id is not None:
        args.append(design_id)
        sql += f' AND notice."design_id" = ${len(args)}'
    sql += ' ORDER BY notice."created_at", notice."id"'
    return await _fetch(unit, sql, *args)


async def claim_notice(notice_id: int, claimed_at: float, *,
                       unit: Unit | None = None) -> Row:
    """Take a notice for delivery, if nobody has it.

    The guard is in the statement and the row comes back with it. A notice
    delivered twice is two harnesses starting work in one working tree, each
    unaware of the other, so the loser is told it lost rather than handed a
    second copy.
    """
    values = {"claimed_at": claimed_at}
    clause, extra = _sql.where(
        NOTICES, {"claimed_at": None, "delivered_at": None},
        start=len(values) + 2)
    sql, args = _sql.update(
        NOTICES, values, {"id": notice_id}, extra_where=clause,
        returning=("id", "design_id", "kind", "page", "body", "created_at",
                   "claimed_at", "delivered_at"))
    return await _execute_returning(unit, sql, *args, *extra)


async def release_notice(notice_id: int, *,
                         unit: Unit | None = None) -> Row:
    """Hand a claimed notice back when the send failed.

    Guarded on it not having been delivered, so an acknowledgement racing a
    failure report cannot un-deliver a notice that landed.
    """
    values: dict[str, Any] = {"claimed_at": None}
    clause, extra = _sql.where(
        NOTICES, {"delivered_at": None}, start=len(values) + 2)
    sql, args = _sql.update(NOTICES, values, {"id": notice_id},
                            extra_where=clause)
    return await _execute(unit, sql, *args, *extra)


async def mark_delivered(notice_id: int, delivered_at: float, *,
                         unit: Unit | None = None) -> Row:
    """Record that a notice was actually sent.

    Guarded on it not having been delivered already: delivery is
    acknowledged after the fact, and a second acknowledgement for one send
    is a second send nobody made.
    """
    values = {"delivered_at": delivered_at}
    clause, extra = _sql.where(
        NOTICES, {"delivered_at": None}, start=len(values) + 2)
    sql, args = _sql.update(NOTICES, values, {"id": notice_id},
                            extra_where=clause)
    return await _execute(unit, sql, *args, *extra)


# ----------------------------------------------------------------- settings

async def set_capability_endpoint(node_id: str, endpoint: str,
                                  updated_at: int, *,
                                  unit: Unit | None = None) -> Row:
    """Point a capability node at an endpoint, replacing whatever it had."""
    sql, args = _sql.insert(CAPABILITIES, {
        "node_id": node_id, "endpoint": endpoint, "updated_at": updated_at,
    }, on_conflict=('ON CONFLICT ("node_id") DO UPDATE SET '
                    '"endpoint" = excluded."endpoint", '
                    '"updated_at" = excluded."updated_at"'))
    return await _execute(unit, sql, *args)


async def capability_endpoint(node_id: str, *,
                              unit: Unit | None = None) -> Row | None:
    """One capability node's endpoint, or None when it has never been set."""
    return await _fetch_one(
        unit,
        f"SELECT {select_list(CAPABILITIES)} FROM {qualified(CAPABILITIES)} "
        'WHERE "node_id" = $1', node_id)


async def clear_capability_endpoint(node_id: str, *,
                                    unit: Unit | None = None) -> Row:
    """Forget a capability node's endpoint, so it falls back to its default."""
    return await _execute(
        unit, f'DELETE FROM {qualified(CAPABILITIES)} WHERE "node_id" = $1',
        node_id)


async def set_edge_mind(name: str, profile: str, role: str, deployment: str,
                        updated_at: int, *,
                        unit: Unit | None = None) -> Row:
    """Record what an edge mind was installed as, replacing what it was."""
    sql, args = _sql.insert(EDGE_MINDS, {
        "name": name, "profile": profile, "role": role,
        "deployment": deployment, "updated_at": updated_at,
    }, on_conflict=('ON CONFLICT ("name") DO UPDATE SET '
                    '"profile" = excluded."profile", '
                    '"role" = excluded."role", '
                    '"deployment" = excluded."deployment", '
                    '"updated_at" = excluded."updated_at"'))
    return await _execute(unit, sql, *args)


async def edge_mind(name: str, *, unit: Unit | None = None) -> Row | None:
    """One edge mind's recorded install, or None."""
    return await _fetch_one(
        unit, f"SELECT {select_list(EDGE_MINDS)} FROM {qualified(EDGE_MINDS)} "
        'WHERE "name" = $1', name)


async def edge_minds(*, unit: Unit | None = None) -> list[Row]:
    """Every edge mind's recorded install, by name."""
    return await _fetch(
        unit, f"SELECT {select_list(EDGE_MINDS)} FROM {qualified(EDGE_MINDS)} "
        'ORDER BY "name"')


# ----------------------------------------------------- who started an operation

async def record_operation_requester(request_id: str, username: str,
                                     created_at: int, *,
                                     unit: Unit | None = None) -> Row:
    """Remember who started an operation, so its status is theirs to read.

    An upsert rather than an insert. The console records the requester from
    whatever a gated call answered with, and a tool that reports the same
    request id twice — a replay, a retry of a webhook — must not turn the
    second report into a refused write on a path whose whole purpose is
    bookkeeping.
    """
    sql, args = _sql.insert(REQUESTERS, {
        "request_id": request_id, "username": username,
        "created_at": created_at,
    }, on_conflict=('ON CONFLICT ("request_id") DO UPDATE SET '
                    '"username" = excluded."username", '
                    '"created_at" = excluded."created_at"'))
    return await _execute(unit, sql, *args)


async def operation_requester(request_id: str, *,
                              unit: Unit | None = None) -> Row | None:
    """The person an operation belongs to, or None when nobody recorded one.

    None is the answer the console's route table wants: an operation with no
    recorded requester belongs to nobody, so only an admin may read it.
    """
    return await _fetch_one(
        unit, f"SELECT {select_list(REQUESTERS)} FROM {qualified(REQUESTERS)} "
        'WHERE "request_id" = $1', request_id)
