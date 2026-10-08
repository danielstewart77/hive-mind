"""The kanban board, on the console's own SQLite database.

One table. An epic, a feature and a story differ by `kind` and by what they
hang off, not by shape, so three tables would be three copies of the same
CRUD and the same cascade. `parent_id` points at the row above: NULL for an
epic, an epic for a feature, and either for a story.

`position` is global and monotonic rather than a count of what is there.
`COALESCE(MAX(position), ...) + 1` over the whole table means a created row
sorts after everything that exists now and after everything that has ever
existed, so deleting from the middle does not hand the next item a slot it
has to share. SQLite leaves ties unordered, so every read sorts by rowid
underneath position and never returns two orders for one board.

The cascade is done here, in Python, and the schema declares no
`ON DELETE CASCADE`. SQLite's `foreign_keys` pragma is off by default and is
per connection, so a declared cascade is decorative on any connection that
forgot the `PRAGMA` — and the orphans it leaves still render on the board
labelled "Unassigned" while being unreachable from the tree that holds the
only delete button. Deleted work that comes back and cannot be removed is
worse than a failed delete. Declaring the cascade *as well* would be worse
still: with both in place neither is load-bearing, so no test can tell
whether either works. Children go first so the foreign key holds at every
step of the delete.
"""
from __future__ import annotations

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
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # WAL so a writer does not block the readers sharing this file — auth and
    # every other console page live in the same database, and a contended
    # board write must not stall login.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS work_items (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            parent_id TEXT REFERENCES work_items(id),
            title TEXT NOT NULL,
            status TEXT,
            position INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS work_items_kind_position"
        " ON work_items (kind, position)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS work_items_parent ON work_items (parent_id)"
    )
    return connection


def _new_id(kind: str) -> str:
    return _PREFIX[kind] + secrets.token_hex(_ID_BYTES)


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("title must be a nonblank string")
    return value.strip()


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
    record: dict[str, Any] = {"id": row["id"], "title": row["title"]}
    if row["kind"] == "feature":
        record["epic_id"] = row["parent_id"]
    elif row["kind"] == "story":
        record[f"{row['parent_kind']}_id"] = row["parent_id"]
        record["status"] = row["status"]
    return record


_SELECT = """
    SELECT item.id, item.kind, item.parent_id, item.title, item.status,
           parent.kind AS parent_kind
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
    with _connect() as connection:
        return {
            "epics": _list(connection, "epic"),
            "features": _list(connection, "feature"),
            "stories": _list(connection, "story"),
        }


def list_epics() -> list[dict[str, Any]]:
    with _connect() as connection:
        return _list(connection, "epic")


def list_features() -> list[dict[str, Any]]:
    with _connect() as connection:
        return _list(connection, "feature")


def list_stories() -> list[dict[str, Any]]:
    with _connect() as connection:
        return _list(connection, "story")


def count_descendants(record_id: str) -> dict[str, int]:
    """How many features and stories a delete of this row would take with it.

    The browser asks right before it puts the number in front of Daniel. Its
    own copy of the board is whatever it loaded, which may be an hour old.
    """
    with _connect() as connection:
        row = connection.execute(
            "SELECT kind FROM work_items WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Item not found: {record_id}")
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
) -> dict[str, Any]:
    record_id = _new_id(kind)
    connection.execute(
        "INSERT INTO work_items (id, kind, parent_id, title, status, position)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (record_id, kind, parent_id, title, status, _next_position(connection)),
    )
    return _row_to_record(_fetch(connection, record_id, kind))


def create_epic(title: str) -> dict[str, Any]:
    checked = _title(title)
    with _connect() as connection:
        _write(connection)
        record = _insert(connection, "epic", checked, None, None)
        connection.execute("COMMIT")
        return record


def create_feature(epic_id: str, title: str) -> dict[str, Any]:
    checked = _title(title)
    with _connect() as connection:
        _write(connection)
        try:
            _fetch(connection, epic_id, "epic")
            record = _insert(connection, "feature", checked, epic_id, None)
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
) -> dict[str, Any]:
    parent_kind, parent_id = _one_parent(feature_id, epic_id)
    checked = _title(title)
    checked_status = _check_status(status)
    with _connect() as connection:
        _write(connection)
        try:
            _fetch(connection, parent_id, parent_kind)
            record = _insert(connection, "story", checked, parent_id, checked_status)
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
    "epic": {"title"},
    "feature": {"title", "epic_id"},
    "story": {"title", "status", "feature_id", "epic_id"},
}


def _update(kind: str, record_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - _ALLOWED_UPDATES[kind]
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown))}")

    with _connect() as connection:
        _write(connection)
        try:
            row = _fetch(connection, record_id, kind)
            assignments: dict[str, Any] = {}

            if "title" in changes:
                assignments["title"] = _title(changes["title"])
            if "status" in changes:
                assignments["status"] = _check_status(changes["status"])

            if kind == "feature" and "epic_id" in changes:
                _fetch(connection, changes["epic_id"], "epic")
                assignments["parent_id"] = changes["epic_id"]
            elif kind == "story" and ("feature_id" in changes or "epic_id" in changes):
                parent_kind, parent_id = _one_parent(
                    changes.get("feature_id"), changes.get("epic_id")
                )
                _fetch(connection, parent_id, parent_kind)
                assignments["parent_id"] = parent_id

            if assignments:
                clause = ", ".join(f"{column} = ?" for column in assignments)
                cursor = connection.execute(
                    f"UPDATE work_items SET {clause} WHERE id = ?",
                    (*assignments.values(), record_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"{_LABEL[kind]} not found: {record_id}")

            record = _row_to_record(_fetch(connection, record_id, kind))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        del row
        return record


def update_epic(record_id: str, **changes: Any) -> dict[str, Any]:
    return _update("epic", record_id, changes)


def update_feature(record_id: str, **changes: Any) -> dict[str, Any]:
    return _update("feature", record_id, changes)


def update_story(record_id: str, **changes: Any) -> dict[str, Any]:
    return _update("story", record_id, changes)


def _delete(kind: str, record_id: str) -> dict[str, Any]:
    with _connect() as connection:
        _write(connection)
        try:
            record = _row_to_record(_fetch(connection, record_id, kind))
            features, stories = _descendants(connection, record_id)
            # Deepest first: the foreign key is enforced per statement, so a
            # parent cannot go while a child still points at it.
            doomed = [*stories, *features, record_id]
            placeholders = ",".join("?" * len(doomed))
            cursor = connection.execute(
                f"DELETE FROM work_items WHERE id IN ({placeholders})", doomed
            )
            if cursor.rowcount == 0:
                raise KeyError(f"{_LABEL[kind]} not found: {record_id}")
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
