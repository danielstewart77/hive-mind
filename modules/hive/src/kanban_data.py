"""The kanban board, on the console's own SQLite database.

One table. An epic, a feature and a story differ by `kind` and by what they
hang off, not by shape, so three tables would be three copies of the same
CRUD and the same cascade. `parent_id` points at the row above: NULL for an
epic, an epic for a feature, and either for a story.

`position` is global and monotonic rather than a count of what is there.
`COALESCE(MAX(position), ...) + 1` over the whole table means a created row
sorts after everything that exists now and after everything that has ever
existed, so deleting from the middle does not hand the next item a slot it
has to share — which is what keeps two rows from sharing a position in the
first place. Reads name `rowid` under `position` because SQL leaves the
order of equal keys undefined; no test covers it, since on this engine the
scan already comes back in that order and no edit here can make it differ.

The cascade is done here, in Python, and the schema declares no
`ON DELETE CASCADE`. SQLite's `foreign_keys` pragma is off by default and is
per connection, so a declared cascade is decorative on any connection that
forgot the `PRAGMA` — and the orphans it leaves still render on the board
labelled "Unassigned" while being unreachable from the tree that holds the
only delete button. Deleted work that comes back and cannot be removed is
worse than a failed delete. Declaring the cascade *as well* would be worse
still: with both in place neither is load-bearing, so no test can tell
whether either works. Parent and descendants go in one statement, and
SQLite checks the foreign key at the end of it, so the order within that
statement decides nothing.
"""
from __future__ import annotations

import contextlib
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent

STATUSES = ("backlog", "committed", "in_progress", "done")

KINDS = ("epic", "feature", "story")
_LABEL = {"epic": "Epic", "feature": "Feature", "story": "Story"}
_COLLECTION = {"epics": "epic", "features": "feature", "stories": "story"}
_PREFIX = {"epic": "e", "feature": "f", "story": "s"}

# The lowest position a created row may take. Anything below it is a row
# somebody placed deliberately, which is what the ordering test writes.
_MIN_POSITION = 1

_ID_BYTES = 8


def _db_path() -> Path:
    return Path(os.getenv("HIVE_DB_PATH", str(BASE_DIR.parent / "data" / "hive.db")))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # No journal-mode change. This file is also opened by auth, design_data
    # and configuration_data with a bare sqlite3.connect, the change is
    # one-way and process-wide, and a busy database returns SQLITE_BUSY to it
    # without invoking the busy handler — so it can silently no-op, or raise
    # on the connect and 500 the board. The timeout matches sqlite3's own
    # default, which is what auth, design_data and configuration_data open
    # this same file on: in rollback-journal mode a writer locks the whole
    # file, so a board transaction allowed to hold longer than they will wait
    # is a failed login write.
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS work_items (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            parent_id TEXT REFERENCES work_items(id),
            title TEXT NOT NULL,
            status TEXT,
            position INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            requirements TEXT NOT NULL DEFAULT '',
            acceptance_criteria TEXT NOT NULL DEFAULT '',
            qa_tests TEXT NOT NULL DEFAULT ''
        )
        """
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(work_items)")}
    for column in ("description", "requirements", "acceptance_criteria", "qa_tests"):
        if column not in columns:
            connection.execute(f"ALTER TABLE work_items ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    if "subtype" not in columns:
        connection.execute("ALTER TABLE work_items ADD COLUMN subtype TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS work_items_kind_position"
        " ON work_items (kind, position)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS work_items_parent ON work_items (parent_id)"
    )
    return connection


@contextlib.contextmanager
def _session():
    """A connection that is actually released when the block ends.

    `with sqlite3.connect(...)` commits; it does not close. The connection
    holds its statement cache in a reference cycle, so refcounting never
    reclaims it and the descriptor waits on the generational collector —
    which an idle process does not run. Under concurrent requests that is
    thousands of open descriptors against a 1024 limit, and the failure is
    not the board's: it is login, and every other page in this process,
    refusing to open the database at all.
    """
    connection = _connect()
    try:
        yield connection
    finally:
        connection.close()


@contextlib.contextmanager
def _snapshot(connection: sqlite3.Connection):
    """One read transaction across several statements.

    Three unguarded reads are three points in time. A write landing between
    them returns a story whose epic is not in the same payload — which the
    board draws as "Unassigned" and the backlog tree, walking down from the
    epics it was given, does not draw at all. That is the orphan this module
    cascades in Python to avoid, reintroduced by the reader.
    """
    connection.execute("BEGIN DEFERRED")
    try:
        yield connection
    finally:
        connection.execute("COMMIT")


def _new_id(kind: str) -> str:
    return _PREFIX[kind] + secrets.token_hex(_ID_BYTES)


# Every title on the board is returned in full on every load, so one pasted
# document is not one bad row, it is a board that is slow for everyone from
# then on, with nothing on screen naming the row responsible.
MAX_TITLE_LENGTH = 500


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("title must be a nonblank string")
    trimmed = value.strip()
    if len(trimmed) > MAX_TITLE_LENGTH:
        raise ValueError(f"title must be at most {MAX_TITLE_LENGTH} characters")
    return trimmed


def _check_status(value: Any) -> str:
    if value not in STATUSES:
        raise ValueError(f"unsupported status: {value}")
    return value


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    """One row as the API has always shaped it.

    The parent comes back under the key naming its kind, and the key for the
    kind it is not is absent rather than null — the browser selects stories
    with `s.epic_id === epic.id`, so a story carrying both keys with one null
    reads as parentless on one view and duplicated on the other.
    """
    record: dict[str, Any] = {
    "id": row["id"],
    "title": row["title"],
    "description": row["description"],
    "requirements": row["requirements"],
    "acceptance_criteria": row["acceptance_criteria"],
    "qa_tests": row["qa_tests"],
}

    if row["kind"] == "feature":
        record["epic_id"] = row["parent_id"]
        record["parent_id"] = row["parent_id"]
    elif row["kind"] == "story":
        if row["parent_kind"]:
            record[f"{row['parent_kind']}_id"] = row["parent_id"]
        record["status"] = row["status"]
        record["subtype"] = row["subtype"] or "story"
    return record


_SELECT = """
    SELECT item.id, item.kind, item.parent_id, item.title, item.status, item.subtype,
           item.description, item.requirements, item.acceptance_criteria, item.qa_tests,
           item.position, parent.kind AS parent_kind
      FROM work_items AS item
      LEFT JOIN work_items AS parent ON parent.id = item.parent_id
"""


def _list(connection: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        _SELECT + " WHERE item.kind = ? ORDER BY item.position, item.rowid", (kind,)
    ).fetchall()
    return [_row_to_record(row) for row in rows]


def _fetch(connection: sqlite3.Connection, record_id: str, kind: str) -> sqlite3.Row:
    row = connection.execute(
        _SELECT + " WHERE item.id = ?", (record_id,)
    ).fetchone()
    if row is None or row["kind"] != kind:
        raise KeyError(f"{_LABEL[kind]} not found: {record_id}")
    return row


def _next_position(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(position), ?) + 1 AS next FROM work_items",
        (_MIN_POSITION - 1,),
    ).fetchone()
    return int(row["next"])


def _ordered_children(connection: sqlite3.Connection, parent_id: str, kind: str) -> list[str]:
    return [row["id"] for row in connection.execute(
        "SELECT id FROM work_items WHERE parent_id = ? AND kind = ? ORDER BY position, rowid",
        (parent_id, kind),
    ).fetchall()]


def _reorder_children(connection: sqlite3.Connection, ids: list[str]) -> None:
    for position, item_id in enumerate(ids, start=_MIN_POSITION):
        connection.execute("UPDATE work_items SET position = ? WHERE id = ?", (position, item_id))


def _move_epic(connection: sqlite3.Connection, item_id: str, before_id: str | None) -> None:
    """Place a top-level epic before another epic, or at the end when none is given."""
    _fetch(connection, item_id, "epic")
    siblings = [row["id"] for row in connection.execute(
        "SELECT id FROM work_items WHERE kind = 'epic' ORDER BY position, rowid"
    ).fetchall()]
    siblings.remove(item_id)
    if before_id is None:
        siblings.append(item_id)
    else:
        if before_id not in siblings:
            raise ValueError("drop target is not an epic")
        siblings.insert(siblings.index(before_id), item_id)
    _reorder_children(connection, siblings)


def _move_child(connection: sqlite3.Connection, item_id: str, kind: str, parent_id: str, before_id: str | None) -> dict[str, Any]:
    row = _fetch(connection, item_id, kind)
    old_parent = row["parent_id"]
    siblings = _ordered_children(connection, parent_id, kind)
    if old_parent == parent_id:
        siblings.remove(item_id)
    if before_id is None:
        index = len(siblings)
    else:
        if before_id not in siblings:
            raise ValueError("drop target is not a child of the destination")
        index = siblings.index(before_id)
    siblings.insert(index, item_id)
    connection.execute("UPDATE work_items SET parent_id = ? WHERE id = ?", (parent_id, item_id))
    _reorder_children(connection, siblings)
    if old_parent is not None and old_parent != parent_id:
        _reorder_children(connection, _ordered_children(connection, old_parent, kind))
    return _row_to_record(_fetch(connection, item_id, kind))


def _write(connection: sqlite3.Connection) -> sqlite3.Connection:
    """Take the write lock up front.

    Every mutation here reads the row, decides against it and writes it back.
    Two of those interleaved is one tab's rename silently replaced by the
    other tab's drag, with both reporting success.
    """
    connection.execute("BEGIN IMMEDIATE")
    return connection


# --- reads -----------------------------------------------------------------


def get_data() -> dict[str, list[dict[str, Any]]]:
    """The whole board.

    All three keys always, even on an empty board: the page builds its lanes
    from `state.epics` and friends before anything is on it, and a missing
    key is a TypeError in the browser with nothing on screen to explain it.
    """
    with _session() as connection, _snapshot(connection):
        return {
            "epics": _list(connection, "epic"),
            "features": _list(connection, "feature"),
            "stories": _list(connection, "story"),
        }


def find_item(record_id: str) -> dict[str, Any]:
    with _session() as connection:
        row = connection.execute(_SELECT + " WHERE item.id = ?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(f"Item not found: {record_id}")
        record = _row_to_record(row)
    # The item page builds its API URL from this kind; the record itself does not carry it.
    record["kind"] = row["kind"]
    return record


def item_relations(record_id: str) -> dict[str, Any]:
    """An item's one parent, its children, and the rows it could be given as a parent."""
    with _session() as connection:
        row = connection.execute(_SELECT + " WHERE item.id = ?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(f"Item not found: {record_id}")
        parent = None
        if row["parent_id"]:
            parent_row = connection.execute(_SELECT + " WHERE item.id = ?", (row["parent_id"],)).fetchone()
            parent = _row_to_record(parent_row)
        children = [
            _row_to_record(child)
            for child in connection.execute(
                _SELECT + " WHERE item.parent_id = ? ORDER BY item.position, item.rowid", (record_id,)
            ).fetchall()
        ]
        # Parents this item may take: any item of equal or higher level, except itself and its own descendants.
        if row["kind"] == "epic":
            # An epic has no parent; a change of type gives it one.
            choices_sql = None
        elif row["kind"] == "feature":
            choices_sql = _SELECT + " WHERE item.kind = 'epic' AND item.id != ?"
        else:
            choices_sql = _SELECT + " WHERE (item.kind = 'feature' OR item.kind = 'epic') AND item.id != ?"
        # Children this item may take: a feature may take stories, an epic may take features or stories.
        if row["kind"] == "epic":
            child_sql = _SELECT + " WHERE item.parent_id IS NULL AND item.kind IN ('feature','story') AND item.id != ?"
        elif row["kind"] == "feature":
            child_sql = _SELECT + " WHERE item.parent_id IS NULL AND item.kind = 'story' AND item.id != ?"
        else:
            child_sql = None
        addable = (
            [_row_to_record(c) for c in connection.execute(child_sql, (record_id,)).fetchall()]
            if child_sql else []
        )
        choices = (
            [_row_to_record(c) for c in connection.execute(choices_sql, (record_id,)).fetchall()]
            if choices_sql else []
        )
        return {
            "parent": parent,
            "children": children,
            "choices": choices,
            "addable": addable,
            "all": [
                _row_to_record(r)
                for r in connection.execute(_SELECT + " WHERE item.id != ? ORDER BY item.kind, item.position, item.rowid", (record_id,)).fetchall()
            ],
        }


def _parents_for(connection: sqlite3.Connection, record_id: str, kind: str) -> list[dict[str, Any]]:
    """Items that could be this item's parent once it has the given type."""
    if kind == "epic":
        return []
    allowed = "('epic')" if kind == "feature" else "('epic','feature')"
    rows = connection.execute(
        _SELECT + f" WHERE item.kind IN {allowed} AND item.id != ? ORDER BY item.position, item.rowid",
        (record_id,),
    ).fetchall()
    return [_row_to_record(r) for r in rows]


def list_epics() -> list[dict[str, Any]]:
    with _session() as connection:
        return _list(connection, "epic")


def list_features() -> list[dict[str, Any]]:
    with _session() as connection:
        return _list(connection, "feature")


def list_stories() -> list[dict[str, Any]]:
    with _session() as connection:
        return _list(connection, "story")


def count_descendants(record_id: str, kind: str | None = None) -> dict[str, int]:
    """How many features and stories a delete of this row would take with it.

    The browser asks right before it puts the number in front of Daniel. Its
    own copy of the board is whatever it loaded, which may be an hour old.
    """
    with _session() as connection:
        row = connection.execute(
            "SELECT kind FROM work_items WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None or (kind is not None and row["kind"] != kind):
            raise KeyError(f"{_LABEL.get(kind, 'Item')} not found: {record_id}")
        features, stories = _descendants(connection, record_id)
        return {"features": len(features), "stories": len(stories)}


def _descendants(
    connection: sqlite3.Connection, record_id: str
) -> tuple[list[str], list[str]]:
    """Every feature and story beneath this row, found by walking the tree.

    Two levels at most today, but walked rather than assumed: a cascade that
    hard-codes the depth silently leaves rows behind the moment the shape
    changes, and those rows are the ones that cannot be deleted afterwards.
    """
    features: list[str] = []
    stories: list[str] = []
    frontier = [record_id]
    seen = {record_id}
    while frontier:
        rows = _children(connection, frontier)
        frontier = []
        for child_id, kind in rows:
            if child_id in seen:
                continue
            seen.add(child_id)
            if kind == "feature":
                features.append(child_id)
            elif kind == "story":
                stories.append(child_id)
            frontier.append(child_id)
    return features, stories


def _children(
    connection: sqlite3.Connection, parents: Iterable[str]
) -> list[tuple[str, str]]:
    parents = list(parents)
    placeholders = ",".join("?" * len(parents))
    rows = connection.execute(
        f"SELECT id, kind FROM work_items WHERE parent_id IN ({placeholders})",
        parents,
    ).fetchall()
    return [(row["id"], row["kind"]) for row in rows]


# --- writes ----------------------------------------------------------------


def _insert(
    connection: sqlite3.Connection,
    kind: str,
    title: str,
    parent_id: str | None,
    status: str | None,
    description: str = "",
    requirements: str = "",
    acceptance_criteria: str = "",
    qa_tests: str = "",
) -> dict[str, Any]:
    record_id = _new_id(kind)
    connection.execute(
        "INSERT INTO work_items (id, kind, parent_id, title, status, description, requirements, acceptance_criteria, qa_tests, position)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (record_id, kind, parent_id, title, status, description, requirements, acceptance_criteria, qa_tests, _next_position(connection)),
    )
    return _row_to_record(_fetch(connection, record_id, kind))


def create_epic(title: str, description: str = "", requirements: str = "", acceptance_criteria: str = "", qa_tests: str = "") -> dict[str, Any]:
    checked = _title(title)
    with _session() as connection:
        _write(connection)
        record = _insert(connection, "epic", checked, None, None, description, requirements, acceptance_criteria, qa_tests)
        connection.execute("COMMIT")
        return record


def create_feature(epic_id: str, title: str, description: str = "", requirements: str = "", acceptance_criteria: str = "", qa_tests: str = "") -> dict[str, Any]:
    checked = _title(title)
    with _session() as connection:
        _write(connection)
        try:
            _fetch(connection, epic_id, "epic")
            record = _insert(connection, "feature", checked, epic_id, None, description, requirements, acceptance_criteria, qa_tests)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return record


def create_story(
    title: str,
    feature_id: str | None = None,
    epic_id: str | None = None,
    status: str = "backlog",
    description: str = "",
    requirements: str = "",
    acceptance_criteria: str = "",
    qa_tests: str = "",
    subtype: str = "story",
) -> dict[str, Any]:
    if subtype not in ("story", "bug"):
        raise ValueError(f"unsupported subtype: {subtype}")
    parent_kind, parent_id = _one_parent(feature_id, epic_id)
    checked = _title(title)
    checked_status = _check_status(status)
    with _session() as connection:
        _write(connection)
        try:
            _fetch(connection, parent_id, parent_kind)
            record = _insert(connection, "story", checked, parent_id, checked_status, description, requirements, acceptance_criteria, qa_tests)
            if subtype != "story":
                connection.execute("UPDATE work_items SET subtype = ? WHERE id = ?", (subtype, record["id"]))
                record = _row_to_record(_fetch(connection, record["id"], "story"))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return record


def _one_parent(
    feature_id: str | None, epic_id: str | None
) -> tuple[str, str]:
    if (feature_id is None) == (epic_id is None):
        raise ValueError("story requires exactly one of feature_id or epic_id")
    if feature_id is not None:
        return "feature", feature_id
    return "epic", epic_id  # type: ignore[return-value]


_ALLOWED_UPDATES = {
    "epic": {"title", "before_id", "description", "requirements", "acceptance_criteria", "qa_tests"},
    "feature": {"title", "epic_id", "before_id", "parent", "description", "requirements", "acceptance_criteria", "qa_tests"},
    "story": {"title", "status", "feature_id", "epic_id", "before_id", "parent", "description", "requirements", "acceptance_criteria", "qa_tests"},
}


def _update(kind: str, record_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - _ALLOWED_UPDATES[kind]
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown))}")

    with _session() as connection:
        _write(connection)
        try:
            row = _fetch(connection, record_id, kind)
            assignments: dict[str, Any] = {}

            if "title" in changes:
                assignments["title"] = _title(changes["title"])
            if "status" in changes:
                assignments["status"] = _check_status(changes["status"])
            if "description" in changes:
                assignments["description"] = str(changes["description"])
            if "requirements" in changes:
                assignments["requirements"] = str(changes["requirements"])
            if "acceptance_criteria" in changes:
                assignments["acceptance_criteria"] = str(changes["acceptance_criteria"])
            if "qa_tests" in changes:
                assignments["qa_tests"] = str(changes["qa_tests"])

            if kind in ("feature", "story") and changes.get("parent") == "none":
                connection.execute(
                    "UPDATE work_items SET parent_id = NULL, position = ? WHERE id = ?",
                    (_next_position(connection), record_id),
                )
                changes = {k: v for k, v in changes.items() if k != "parent"}

            if kind == "epic" and "before_id" in changes:
                _move_epic(connection, record_id, changes.get("before_id"))
            if kind == "feature" and ("epic_id" in changes or "before_id" in changes):
                parent_id = changes.get("epic_id", row["parent_id"])
                _fetch(connection, parent_id, "epic")
                record = _move_child(connection, record_id, kind, parent_id, changes.get("before_id"))
                if assignments:
                    clause = ", ".join(f"{column} = ?" for column in assignments)
                    connection.execute(f"UPDATE work_items SET {clause} WHERE id = ?", (*assignments.values(), record_id))
                assignments = {}
            elif kind == "story" and ("feature_id" in changes or "epic_id" in changes or "before_id" in changes):
                if "feature_id" not in changes and "epic_id" not in changes:
                    parent_id = row["parent_id"]
                    parent_kind = row["parent_kind"]
                else:
                    parent_kind, parent_id = _one_parent(
                        changes.get("feature_id"), changes.get("epic_id")
                    )
                _fetch(connection, parent_id, parent_kind)
                record = _move_child(connection, record_id, kind, parent_id, changes.get("before_id"))
                if assignments:
                    clause = ", ".join(f"{column} = ?" for column in assignments)
                    connection.execute(f"UPDATE work_items SET {clause} WHERE id = ?", (*assignments.values(), record_id))
                assignments = {}

            if assignments:
                clause = ", ".join(f"{column} = ?" for column in assignments)
                connection.execute(
                    f"UPDATE work_items SET {clause} WHERE id = ?",
                    (*assignments.values(), record_id),
                )

            record = _row_to_record(_fetch(connection, record_id, kind))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        del row
        return record




def change_type(record_id: str, new_kind: str, parent_id: str | None = None, clear_parent: bool = False) -> dict[str, Any]:
    """Set an item's type and parent as chosen. Nothing is refused for its level or its children.

    The only checks are that the item exists, the parent (if any) exists, and
    an item is not made its own parent. A bug is a story with the bug subtype.
    An epic with a parent keeps that parent in the row; the page is what offers
    the choice, and the store keeps what it is given.
    """
    if new_kind not in KINDS + ("bug",):
        raise ValueError(f"unsupported type: {new_kind}")
    target_kind = "story" if new_kind == "bug" else new_kind
    subtype = "bug" if new_kind == "bug" else ""
    with _session() as connection:
        _write(connection)
        try:
            row = connection.execute(_SELECT + " WHERE item.id = ?", (record_id,)).fetchone()
            if row is None:
                raise KeyError(f"Item not found: {record_id}")
            if parent_id is not None:
                if parent_id == record_id:
                    raise ValueError("An item cannot be its own parent")
                if connection.execute("SELECT 1 FROM work_items WHERE id = ?", (parent_id,)).fetchone() is None:
                    raise KeyError(f"Item not found: {parent_id}")
                new_parent = parent_id
            elif clear_parent or target_kind == "epic":
                new_parent = None
            else:
                new_parent = row["parent_id"]
            status = None if target_kind in ("epic", "feature") else (row["status"] or "backlog")
            connection.execute(
                "UPDATE work_items SET kind = ?, parent_id = ?, status = ?, subtype = ?, position = ? WHERE id = ?",
                (target_kind, new_parent, status, subtype, _next_position(connection), record_id),
            )
            record = _row_to_record(connection.execute(_SELECT + " WHERE item.id = ?", (record_id,)).fetchone())
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return record


def current_type(record_id: str) -> str:
    """The type a row has now, as change_type accepts it: a bug stays a bug."""
    with _session() as connection:
        row = connection.execute(_SELECT + " WHERE item.id = ?", (record_id,)).fetchone()
    if row is None:
        raise KeyError(f"Item not found: {record_id}")
    if row["kind"] == "story" and (row["subtype"] or "") == "bug":
        return "bug"
    return row["kind"]


def set_parent(record_id: str, parent_id: str | None) -> dict[str, Any]:
    """Make parent_id the parent of record_id, or clear it when parent_id is None.

    No rule about kind or level. Only the item moved changes; its children keep
    their own parent links, which is the point of a parent-only move.
    """
    if parent_id == record_id:
        raise ValueError("An item cannot be its own parent")
    with _session() as connection:
        _write(connection)
        try:
            if connection.execute("SELECT 1 FROM work_items WHERE id = ?", (record_id,)).fetchone() is None:
                raise KeyError(f"Item not found: {record_id}")
            if parent_id is not None and connection.execute(
                "SELECT 1 FROM work_items WHERE id = ?", (parent_id,)
            ).fetchone() is None:
                raise KeyError(f"Item not found: {parent_id}")
            connection.execute(
                "UPDATE work_items SET parent_id = ?, position = ? WHERE id = ?",
                (parent_id, _next_position(connection), record_id),
            )
            record = _row_to_record(connection.execute(_SELECT + " WHERE item.id = ?", (record_id,)).fetchone())
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return record


def kind_of(record_id: str) -> str:
    """The kind a row is now, which may differ from the kind its URL was built with."""
    with _session() as connection:
        row = connection.execute("SELECT kind FROM work_items WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise KeyError(f"Item not found: {record_id}")
    return row["kind"]


def update_epic(record_id: str, **changes: Any) -> dict[str, Any]:
    return _update("epic", record_id, changes)


def update_feature(record_id: str, **changes: Any) -> dict[str, Any]:
    return _update("feature", record_id, changes)


def update_story(record_id: str, **changes: Any) -> dict[str, Any]:
    return _update("story", record_id, changes)


def _delete(kind: str, record_id: str) -> dict[str, Any]:
    with _session() as connection:
        _write(connection)
        try:
            record = _row_to_record(_fetch(connection, record_id, kind))
            features, stories = _descendants(connection, record_id)
            doomed = [record_id, *features, *stories]
            placeholders = ",".join("?" * len(doomed))
            connection.execute(
                f"DELETE FROM work_items WHERE id IN ({placeholders})", doomed
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    # What went, not what the caller's page thought would go.
    record["removed"] = {"features": len(features), "stories": len(stories)}
    return record


def delete_epic(record_id: str) -> dict[str, Any]:
    return _delete("epic", record_id)


def delete_feature(record_id: str) -> dict[str, Any]:
    return _delete("feature", record_id)


def delete_story(record_id: str) -> dict[str, Any]:
    return _delete("story", record_id)
