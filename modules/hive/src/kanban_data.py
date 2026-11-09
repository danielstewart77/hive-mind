"""The kanban board: one shape, three levels, and the rules over the store.

An epic, a feature and a story differ by `kind` and by what they hang off,
not by shape, so three tables would be three copies of the same CRUD and the
same cascade. `parent_id` points at the row above: NULL for an epic, an epic
for a feature, and either for a story.

No SQL lives here. Every read and write goes through `store`, which answers
from the hive's Postgres, so the rules below are decided once, here.

`position` is global and monotonic rather than a count of what is there.
`store.next_position` is `MAX(position) + 1` over the whole table, so a
created row sorts after everything that exists now and after everything that
has ever existed: deleting from the middle does not hand the next item a slot
it has to share, which is what keeps two rows from sharing a position in the
first place.

Every mutation runs inside `store.board_transaction()`, which serialises the
board against every other write to it. Each one reads the sibling list,
decides a new numbering against it and writes the whole list back, so two
interleaved is one tab's rename silently replaced by the other tab's drag,
with both reporting success and two siblings left holding one position.

The cascade is the store's: the foreign key is real, deferred, and the
delete is one recursive statement, so no orphan is left rendering on the
board labelled "Unassigned" while being unreachable from the tree that holds
the only delete button.
"""
from __future__ import annotations

import secrets
from typing import Any

import store

STATUSES = ("backlog", "committed", "in_progress", "done")

KINDS = ("epic", "feature", "story")
_LABEL = {"epic": "Epic", "feature": "Feature", "story": "Story"}
_PREFIX = {"epic": "e", "feature": "f", "story": "s"}
#: The three collections the board payload has always been shaped as. Named
#: rather than pluralised from the kind: "story" pluralises to "stories" and
#: a trailing "s" makes it "storys", which is a key no page reads.
_COLLECTION = {"epic": "epics", "feature": "features", "story": "stories"}

_ID_BYTES = 8

# Every title on the board is returned in full on every load, so one pasted
# document is not one bad row, it is a board that is slow for everyone from
# then on, with nothing on screen naming the row responsible.
MAX_TITLE_LENGTH = 500


def _new_id(kind: str) -> str:
    return _PREFIX[kind] + secrets.token_hex(_ID_BYTES)


#: What a text column will not take. Refused here rather than at the store:
#: Postgres cannot encode a NUL byte at all, and left to the engine the paste
#: is an unhandled error rather than a refusal that says what was wrong.
#:
#: Refused rather than stripped, for the reason the hive refuses it
#: elsewhere: a title or a requirement quietly altered on its way in is a
#: document that does not say what its author typed, and nothing on the page
#: says so.
def _no_nul(field: str, value: str) -> str:
    if "\x00" in value:
        raise ValueError(
            f"{field} contains a NUL byte, which no text column here will "
            "take. It is refused rather than stripped: a document quietly "
            "altered on its way in is one that does not say what was typed.")
    return value


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("title must be a nonblank string")
    trimmed = _no_nul("title", value.strip())
    if len(trimmed) > MAX_TITLE_LENGTH:
        raise ValueError(f"title must be at most {MAX_TITLE_LENGTH} characters")
    return trimmed


def _check_status(value: Any) -> str:
    if value not in STATUSES:
        raise ValueError(f"unsupported status: {value}")
    return value


def _row_to_record(row: dict[str, Any]) -> dict[str, Any]:
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


async def _fetch(record_id: str, kind: str) -> dict[str, Any]:
    row = await store.work_item(record_id)
    if row is None or row["kind"] != kind:
        raise KeyError(f"{_LABEL[kind]} not found: {record_id}")
    return row


async def _exists(record_id: str) -> bool:
    return await store.work_item(record_id) is not None


async def _ordered_children(parent_id: str, kind: str) -> list[str]:
    return [row["id"] for row in await store.children(parent_id, kind)]


async def _move_epic(item_id: str, before_id: str | None) -> None:
    """Place a top-level epic before another epic, or at the end when none."""
    await _fetch(item_id, "epic")
    siblings = [row["id"] for row in await store.work_items("epic")]
    siblings.remove(item_id)
    if before_id is None:
        siblings.append(item_id)
    else:
        if before_id not in siblings:
            raise ValueError("drop target is not an epic")
        siblings.insert(siblings.index(before_id), item_id)
    await store.renumber(siblings)


async def _move_child(item_id: str, kind: str, parent_id: str,
                      before_id: str | None) -> dict[str, Any]:
    row = await _fetch(item_id, kind)
    old_parent = row["parent_id"]
    siblings = await _ordered_children(parent_id, kind)
    if old_parent == parent_id:
        siblings.remove(item_id)
    if before_id is None:
        index = len(siblings)
    else:
        if before_id not in siblings:
            raise ValueError("drop target is not a child of the destination")
        index = siblings.index(before_id)
    siblings.insert(index, item_id)
    # The parent moves with a position rather than on its own: a row whose
    # parent changed and whose position did not is holding a number that was
    # only unique under the parent it left.
    await store.place(item_id, parent_id, store.MIN_POSITION + index)
    await store.renumber(siblings)
    if old_parent is not None and old_parent != parent_id:
        await store.renumber(await _ordered_children(old_parent, kind))
    return _row_to_record(await _fetch(item_id, kind))


# --- reads -----------------------------------------------------------------


def _numbering_parent(record: dict[str, Any]) -> str | None:
    """The id a row is numbered under, or None when it numbers as a root.

    Two cases are arguably wrong and are kept deliberately, because every
    story's number appears in conversations, commit messages and the lucent
    graph, so a change here renames work. An **epic** numbers as a root
    whatever `parent_id` holds — `change_type` can give one a parent, and the
    record an epic renders carries no parent key at all. A **story under a
    story** numbers as a root too: its record carries the parent under
    `story_id`, which this reads nothing from.
    """
    return (record.get("parent_id") or record.get("feature_id")
            or record.get("epic_id"))


def _numbers_for(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Every row's hierarchy number, from rows already in board order."""
    records = [_row_to_record(row) | {"kind": row["kind"]} for row in rows]
    children: dict[str | None, list[dict[str, Any]]] = {}
    for record in records:
        children.setdefault(_numbering_parent(record), []).append(record)

    numbers: dict[str, str] = {}

    def number_tree(parent_id: str | None, prefix: str = "") -> None:
        for index, record in enumerate(children.get(parent_id, []), start=1):
            number = f"{prefix}.{index}" if prefix else str(index)
            numbers[record["id"]] = number
            number_tree(record["id"], number)

    number_tree(None)
    for record in records:
        numbers.setdefault(record["id"], "")
    return numbers


async def backlog_numbers() -> dict[str, str]:
    """Every row's number, from one read of the whole board.

    One read, so the numbers are all from the same moment. Numbered from two,
    a row created between them is a tree missing the parent its children hang
    off, and every number below that parent shifts.
    """
    return _numbers_for(await store.work_items())


async def get_data() -> dict[str, list[dict[str, Any]]]:
    """The whole board with the three established record collections.

    One read of every row rather than three of one kind each. Three reads are
    three points in time, and a write landing between them returns a story
    whose epic is not in the same payload — which the board draws as
    "Unassigned" and the backlog tree, walking down from the epics it was
    given, does not draw at all.
    """
    rows = await store.work_items()
    out: dict[str, list[dict[str, Any]]] = {
        "epics": [], "features": [], "stories": []}
    for row in rows:
        out[_COLLECTION[row["kind"]]].append(_row_to_record(row))
    return out


async def get_backlog_number(record_id: str) -> str:
    numbers = await backlog_numbers()
    if record_id not in numbers:
        raise KeyError(f"Item not found: {record_id}")
    return numbers[record_id]


async def find_item(record_id: str) -> dict[str, Any]:
    """One item, its kind, and its number, from one read of the board.

    The number needs every row anyway, so the item is taken out of that same
    read rather than fetched beside it — two reads are two boards, and the
    number would be computed against one the item may not be in.
    """
    rows = await store.work_items()
    row = next((r for r in rows if r["id"] == record_id), None)
    if row is None:
        raise KeyError(f"Item not found: {record_id}")
    record = _row_to_record(row)
    # The item page builds its API URL from this kind; the record itself
    # does not carry it.
    record["kind"] = row["kind"]
    record["backlog_number"] = _numbers_for(rows)[record_id]
    return record


async def item_relations(record_id: str) -> dict[str, Any]:
    """An item's one parent, its children, and the rows it could be given.

    From one read of the board, for the reason `get_data` gives: every list
    below is a slice of the same rows, so they cannot disagree about what is
    on the board.
    """
    rows = await store.work_items()
    by_id = {row["id"]: row for row in rows}
    row = by_id.get(record_id)
    if row is None:
        raise KeyError(f"Item not found: {record_id}")

    parent = None
    if row["parent_id"] and row["parent_id"] in by_id:
        parent = _row_to_record(by_id[row["parent_id"]])
    children = [_row_to_record(r) for r in rows
                if r["parent_id"] == record_id]

    # Parents this item may take: any item of equal or higher level, except
    # itself. Children it may take: a feature may take stories, an epic may
    # take features or stories.
    if row["kind"] == "epic":
        # An epic has no parent; a change of type gives it one.
        choices: list[dict[str, Any]] = []
        addable = [_row_to_record(r) for r in rows
                   if r["parent_id"] is None
                   and r["kind"] in ("feature", "story")
                   and r["id"] != record_id]
    elif row["kind"] == "feature":
        choices = [_row_to_record(r) for r in rows
                   if r["kind"] == "epic" and r["id"] != record_id]
        addable = [_row_to_record(r) for r in rows
                   if r["parent_id"] is None and r["kind"] == "story"
                   and r["id"] != record_id]
    else:
        choices = [_row_to_record(r) for r in rows
                   if r["kind"] in ("feature", "epic")
                   and r["id"] != record_id]
        addable = []

    return {
        "parent": parent,
        "children": children,
        "choices": choices,
        "addable": addable,
        "all": [_row_to_record(r) for r in
                sorted(rows, key=lambda r: (r["kind"], r["position"],
                                            str(r["id"])))
                if r["id"] != record_id],
    }


async def _list(kind: str) -> list[dict[str, Any]]:
    return [_row_to_record(row) for row in await store.work_items(kind)]


async def list_epics() -> list[dict[str, Any]]:
    return await _list("epic")


async def list_features() -> list[dict[str, Any]]:
    return await _list("feature")


async def list_stories() -> list[dict[str, Any]]:
    return await _list("story")


async def count_descendants(record_id: str,
                            kind: str | None = None) -> dict[str, int]:
    """How many features and stories a delete of this row would take with it.

    The browser asks right before it puts the number in front of the
    operator. Its own copy of the board is whatever it loaded, which may be
    an hour old.
    """
    row = await store.work_item(record_id)
    if row is None or (kind is not None and row["kind"] != kind):
        label = _LABEL.get(kind or "", "Item")
        raise KeyError(f"{label} not found: {record_id}")
    below = await store.descendants(record_id)
    return {
        "features": sum(1 for r in below if r["kind"] == "feature"),
        "stories": sum(1 for r in below if r["kind"] == "story"),
    }


async def kind_of(record_id: str) -> str:
    """The kind a row is now, which may differ from the kind its URL was
    built with."""
    row = await store.work_item(record_id)
    if row is None:
        raise KeyError(f"Item not found: {record_id}")
    return str(row["kind"])


async def current_type(record_id: str) -> str:
    """The type a row has now, as change_type accepts it: a bug stays a bug."""
    row = await store.work_item(record_id)
    if row is None:
        raise KeyError(f"Item not found: {record_id}")
    if row["kind"] == "story" and (row["subtype"] or "") == "bug":
        return "bug"
    return str(row["kind"])


# --- writes ----------------------------------------------------------------


async def _insert(kind: str, title: str, parent_id: str | None,
                  status: str | None, position: int, *,
                  subtype: str = "", description: str = "",
                  requirements: str = "", acceptance_criteria: str = "",
                  qa_tests: str = "") -> dict[str, Any]:
    description = _no_nul("description", description)
    requirements = _no_nul("requirements", requirements)
    acceptance_criteria = _no_nul("acceptance_criteria", acceptance_criteria)
    qa_tests = _no_nul("qa_tests", qa_tests)
    record_id = _new_id(kind)
    await store.create_work_item(
        record_id, kind, title, position, parent_id=parent_id, status=status,
        subtype=subtype, description=description, requirements=requirements,
        acceptance_criteria=acceptance_criteria, qa_tests=qa_tests)
    return _row_to_record(await _fetch(record_id, kind))


async def create_epic(title: str, description: str = "",
                      requirements: str = "", acceptance_criteria: str = "",
                      qa_tests: str = "") -> dict[str, Any]:
    """A new epic, at the top of the board.

    The position is read and the row written inside one unit of work: read
    outside it, two creations land on the same slot.
    """
    checked = _title(title)
    async with store.board_transaction():
        return await _insert(
            "epic", checked, None, None, await store.first_position(),
            description=description, requirements=requirements,
            acceptance_criteria=acceptance_criteria, qa_tests=qa_tests)


async def create_feature(epic_id: str, title: str, description: str = "",
                         requirements: str = "",
                         acceptance_criteria: str = "",
                         qa_tests: str = "") -> dict[str, Any]:
    checked = _title(title)
    async with store.board_transaction():
        await _fetch(epic_id, "epic")
        return await _insert(
            "feature", checked, epic_id, None, await store.next_position(),
            description=description, requirements=requirements,
            acceptance_criteria=acceptance_criteria, qa_tests=qa_tests)


async def create_story(title: str, feature_id: str | None = None,
                       epic_id: str | None = None, status: str = "backlog",
                       description: str = "", requirements: str = "",
                       acceptance_criteria: str = "", qa_tests: str = "",
                       subtype: str = "story") -> dict[str, Any]:
    if subtype not in ("story", "bug"):
        raise ValueError(f"unsupported subtype: {subtype}")
    parent_kind, parent_id = _one_parent(feature_id, epic_id)
    checked = _title(title)
    checked_status = _check_status(status)
    async with store.board_transaction():
        await _fetch(parent_id, parent_kind)
        return await _insert(
            "story", checked, parent_id, checked_status,
            await store.next_position(),
            subtype="" if subtype == "story" else subtype,
            description=description, requirements=requirements,
            acceptance_criteria=acceptance_criteria, qa_tests=qa_tests)


def _one_parent(feature_id: str | None,
                epic_id: str | None) -> tuple[str, str]:
    if (feature_id is None) == (epic_id is None):
        raise ValueError("story requires exactly one of feature_id or epic_id")
    if feature_id is not None:
        return "feature", feature_id
    return "epic", epic_id  # type: ignore[return-value]


_ALLOWED_UPDATES = {
    "epic": {"title", "before_id", "description", "requirements",
             "acceptance_criteria", "qa_tests"},
    "feature": {"title", "epic_id", "before_id", "parent", "description",
                "requirements", "acceptance_criteria", "qa_tests"},
    "story": {"title", "status", "feature_id", "epic_id", "before_id",
              "parent", "description", "requirements", "acceptance_criteria",
              "qa_tests"},
}

#: The prose fields, which are taken as given. A page is a document and the
#: store keeps what it is handed.
_PROSE = ("description", "requirements", "acceptance_criteria", "qa_tests")


async def _update(kind: str, record_id: str,
                  changes: dict[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - _ALLOWED_UPDATES[kind]
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown))}")

    async with store.board_transaction():
        row = await _fetch(record_id, kind)
        assignments: dict[str, Any] = {}

        if "title" in changes:
            assignments["title"] = _title(changes["title"])
        if "status" in changes:
            assignments["status"] = _check_status(changes["status"])
        for field in _PROSE:
            if field in changes:
                assignments[field] = _no_nul(field, str(changes[field]))

        if kind in ("feature", "story") and changes.get("parent") == "none":
            await store.place(record_id, None, await store.next_position())
            changes = {k: v for k, v in changes.items() if k != "parent"}

        if kind == "epic" and "before_id" in changes:
            await _move_epic(record_id, changes.get("before_id"))
        if kind == "feature" and ("epic_id" in changes
                                 or "before_id" in changes):
            parent_id = changes.get("epic_id", row["parent_id"])
            await _fetch(parent_id, "epic")
            await _move_child(record_id, kind, parent_id,
                              changes.get("before_id"))
        elif kind == "story" and ("feature_id" in changes
                                  or "epic_id" in changes
                                  or "before_id" in changes):
            if "feature_id" not in changes and "epic_id" not in changes:
                parent_id = row["parent_id"]
                parent_kind = row["parent_kind"]
            else:
                parent_kind, parent_id = _one_parent(
                    changes.get("feature_id"), changes.get("epic_id"))
            await _fetch(parent_id, parent_kind)
            await _move_child(record_id, kind, parent_id,
                              changes.get("before_id"))

        if assignments:
            await store.update_work_item(record_id, assignments)

        return _row_to_record(await _fetch(record_id, kind))


async def update_epic(record_id: str, **changes: Any) -> dict[str, Any]:
    return await _update("epic", record_id, changes)


async def update_feature(record_id: str, **changes: Any) -> dict[str, Any]:
    return await _update("feature", record_id, changes)


async def update_story(record_id: str, **changes: Any) -> dict[str, Any]:
    return await _update("story", record_id, changes)


async def change_type(record_id: str, new_kind: str,
                      parent_id: str | None = None,
                      clear_parent: bool = False) -> dict[str, Any]:
    """Set an item's type and parent as chosen. Nothing is refused for its
    level or its children.

    The only checks are that the item exists, the parent (if any) exists, and
    an item is not made its own parent. A bug is a story with the bug
    subtype. An epic with a parent keeps that parent in the row; the page is
    what offers the choice, and the store keeps what it is given.
    """
    if new_kind not in KINDS + ("bug",):
        raise ValueError(f"unsupported type: {new_kind}")
    target_kind = "story" if new_kind == "bug" else new_kind
    subtype = "bug" if new_kind == "bug" else ""
    async with store.board_transaction():
        row = await store.work_item(record_id)
        if row is None:
            raise KeyError(f"Item not found: {record_id}")
        if parent_id is not None:
            if parent_id == record_id:
                raise ValueError("An item cannot be its own parent")
            if not await _exists(parent_id):
                raise KeyError(f"Item not found: {parent_id}")
            new_parent = parent_id
        elif clear_parent or target_kind == "epic":
            new_parent = None
        else:
            new_parent = row["parent_id"]
        status = (None if target_kind in ("epic", "feature")
                  else (row["status"] or "backlog"))
        await store.retype(record_id, target_kind, status=status,
                           subtype=subtype, parent_id=new_parent,
                           position=await store.next_position())
        moved = await store.work_item(record_id)
        assert moved is not None  # it was read above, inside this unit
        return _row_to_record(moved)


async def place_before(record_id: str, before_id: str) -> dict[str, Any]:
    """Put record_id directly before before_id, under before_id's parent.

    A reorder drop: the moved item takes the same parent as the item it lands
    in front of, and nothing else changes. Its own children are not touched.
    """
    if before_id == record_id:
        raise ValueError("An item cannot be placed before itself")
    async with store.board_transaction():
        target = await store.work_item(before_id)
        if target is None:
            raise KeyError(f"Item not found: {before_id}")
        if not await _exists(record_id):
            raise KeyError(f"Item not found: {record_id}")
        siblings = [row["id"] for row in
                    await store.children(target["parent_id"])
                    if row["id"] != record_id]
        siblings.insert(siblings.index(before_id), record_id)
        await store.place(record_id, target["parent_id"],
                          store.MIN_POSITION + siblings.index(record_id))
        await store.renumber(siblings)
        moved = await store.work_item(record_id)
        assert moved is not None
        return _row_to_record(moved)


async def set_parent(record_id: str,
                     parent_id: str | None) -> dict[str, Any]:
    """Make parent_id the parent of record_id, or clear it when None.

    No rule about kind or level. Only the item moved changes; its children
    keep their own parent links, which is the point of a parent-only move.
    """
    if parent_id == record_id:
        raise ValueError("An item cannot be its own parent")
    async with store.board_transaction():
        if not await _exists(record_id):
            raise KeyError(f"Item not found: {record_id}")
        if parent_id is not None and not await _exists(parent_id):
            raise KeyError(f"Item not found: {parent_id}")
        await store.place(record_id, parent_id, await store.next_position())
        moved = await store.work_item(record_id)
        assert moved is not None
        return _row_to_record(moved)


async def _delete(kind: str, record_id: str) -> dict[str, Any]:
    async with store.board_transaction():
        record = _row_to_record(await _fetch(record_id, kind))
        below = await store.descendants(record_id)
        await store.delete_subtree(record_id)
    # What went, not what the caller's page thought would go.
    record["removed"] = {
        "features": sum(1 for r in below if r["kind"] == "feature"),
        "stories": sum(1 for r in below if r["kind"] == "story"),
    }
    return record


async def delete_epic(record_id: str) -> dict[str, Any]:
    return await _delete("epic", record_id)


async def delete_feature(record_id: str) -> dict[str, Any]:
    return await _delete("feature", record_id)


async def delete_story(record_id: str) -> dict[str, Any]:
    return await _delete("story", record_id)
