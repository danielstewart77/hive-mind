"""The kanban board: one shape, four kinds, and the rules over the store.

An epic, a feature, a story and a task differ by `kind` and by what they hang
off, not by shape, so four tables would be four copies of the same CRUD and
the same cascade. `parent_id` points at the row above: NULL for an epic, an
epic for a feature, either for a story, and anything or nothing for a task -
the one kind made to stand alone (story 3.7.4).

Every kind carries a status, one of `STATUSES` (story 3.7.1): an epic or a
feature can be done, not only a story. The store holds it NOT NULL and
defaults it to backlog (hive-data step 13).

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
import unicodedata
from typing import Any

import store

STATUSES = ("backlog", "committed", "in_progress", "done")

KINDS = ("epic", "feature", "story", "task")
_LABEL = {"epic": "Epic", "feature": "Feature", "story": "Story",
          "task": "Task"}
_PREFIX = {"epic": "e", "feature": "f", "story": "s", "task": "t"}
#: The collections the board payload is shaped as, one per kind. Named
#: rather than pluralised from the kind: "story" pluralises to "stories" and
#: a trailing "s" makes it "storys", which is a key no page reads.
_COLLECTION = {"epic": "epics", "feature": "features", "story": "stories",
               "task": "tasks"}

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
        "status": row["status"],
    }

    if row["kind"] == "feature":
        record["epic_id"] = row["parent_id"]
        record["parent_id"] = row["parent_id"]
    elif row["kind"] == "story":
        if row["parent_kind"]:
            record[f"{row['parent_kind']}_id"] = row["parent_id"]
        record["subtype"] = row["subtype"] or "story"
    elif row["kind"] == "task":
        # Under `parent_id` whatever it hangs off, and absent when it stands
        # alone: a task's parent may be any kind, so no kind-named key fits.
        if row["parent_id"]:
            record["parent_id"] = row["parent_id"]
    return record


async def _fetch(record_id: str, kind: str) -> dict[str, Any]:
    row = await store.work_item(record_id)
    if row is None or row["kind"] != kind:
        raise KeyError(f"{_LABEL[kind]} not found: {record_id}")
    return row


async def _exists(record_id: str) -> bool:
    return await store.work_item(record_id) is not None


def _numbered_list(rows: list[dict[str, Any]],
                   parent_id: str | None) -> list[str]:
    """The ids numbered under parent_id, in board order, whatever their kind.

    This is the list an order number counts along, so it is the list an
    order write renumbers. Not the rows of one kind: an epic's features and
    its stories are one list (1.1 a feature, 1.2 a story), and renumbering
    only the features hands them 1..n beside stories already holding 1..m,
    so two rows share a position and the tie is settled by a random id —
    every number in that list shifts and nobody moved anything. Not the rows
    of one stored parent either: an epic numbers as a root whatever its
    `parent_id` holds, and so does a story under a story.
    """
    return [row["id"] for row in rows
            if _numbering_parent(_row_to_record(row)) == parent_id]


def _refuse_cycle(rows: list[dict[str, Any]], record_id: str,
                  parent_id: str | None) -> None:
    """Refuse a parent that sits beneath the item being moved.

    The numbering, the tree and the cascade all walk down from the roots. An
    item made the child of its own child is a loop nothing reaches: the item
    and everything under it lose their numbers and drop out of the tree that
    holds their only delete button, while the drop reports success.
    """
    by_id = {row["id"]: row for row in rows}
    seen: set[str] = set()
    current = parent_id
    while current is not None and current not in seen:
        if current == record_id:
            raise ValueError(
                "An item cannot be moved under an item that is beneath it: "
                "the two would hang off each other and drop out of the board")
        seen.add(current)
        row = by_id.get(current)
        current = row["parent_id"] if row else None


async def _settle(before: list[dict[str, Any]], placed: str) -> None:
    """Send every row whose numbered list a write changed to the end of it.

    A retype or a parent change moves more than the item it names: a feature
    retyped to a story takes its stories into the top-level list (a story
    under a story numbers as a root), still holding positions from the list
    they left. Left there they land among the epics wherever those stale
    numbers fall, and epics nobody touched change number. They go last
    instead, in the order they had, after the item the write placed.
    """
    was = {row["id"]: _numbering_parent(_row_to_record(row)) for row in before}
    rows = await store.work_items()
    shifted = [row["id"] for row in rows
               if row["id"] != placed and row["id"] in was
               and was[row["id"]] != _numbering_parent(_row_to_record(row))]
    if shifted:
        await store.renumber(shifted, start=await store.next_position())


async def _place_in_list(item_id: str, parent_id: str | None,
                         before_id: str | None) -> None:
    """Give item_id the stored parent parent_id, and put it directly before
    before_id in the list it now numbers in — or last when before_id is None
    or is not in that list.

    The whole numbered list is written back, so every position in it is
    distinct and the order is exactly the one decided here.
    """
    rows = await store.work_items()
    _refuse_cycle(rows, item_id, parent_id)
    await store.place(item_id, parent_id, store.MIN_POSITION)
    rows = await store.work_items()
    moved = next(row for row in rows if row["id"] == item_id)
    siblings = [i for i in _numbered_list(
        rows, _numbering_parent(_row_to_record(moved))) if i != item_id]
    if before_id in siblings:
        siblings.insert(siblings.index(before_id), item_id)
    else:
        siblings.append(item_id)
    await store.renumber(siblings)


async def _move_epic(item_id: str, before_id: str | None) -> None:
    """Place an epic before another top-level item, or at the end when none."""
    row = await _fetch(item_id, "epic")
    if before_id is not None and before_id not in _numbered_list(
            await store.work_items(), None):
        raise ValueError("drop target is not a top-level item")
    await _place_in_list(item_id, row["parent_id"], before_id)


async def _move_child(item_id: str, kind: str, parent_id: str,
                      before_id: str | None) -> dict[str, Any]:
    await _fetch(item_id, kind)
    if before_id is not None and before_id not in _numbered_list(
            await store.work_items(), parent_id):
        raise ValueError("drop target is not a child of the destination")
    # The parent moves with a position rather than on its own: a row whose
    # parent changed and whose position did not is holding a number that was
    # only unique under the parent it left.
    await _place_in_list(item_id, parent_id, before_id)
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
    `story_id`, which this reads nothing from. A story under a **task**
    numbers beneath the task (`task_id`): no such row predates tasks, so
    reading it renames nothing, and the backlog draws it there.
    """
    return (record.get("parent_id") or record.get("feature_id")
            or record.get("epic_id") or record.get("task_id"))


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
    """The whole board, every status included, one collection per kind.

    Done items are here: the status filter that hides them is the backlog
    page's, and the Chief of Staff's done barrel, a mind holding the board
    bearer and the one-database check all read this.

    One read of every row rather than three of one kind each. Three reads are
    three points in time, and a write landing between them returns a story
    whose epic is not in the same payload — which the board draws as
    "Unassigned" and the backlog tree, walking down from the epics it was
    given, does not draw at all.
    """
    rows = await store.work_items()
    out: dict[str, list[dict[str, Any]]] = {
        name: [] for name in _COLLECTION.values()}
    for row in rows:
        out[_COLLECTION[row["kind"]]].append(_row_to_record(row))
    return out


async def get_board() -> dict[str, Any]:
    """The board as `get_data` shapes it, with every row's order number
    under "numbers", from one read.

    One read, so the order the page draws in and the numbers it labels with
    are the same moment: fetched as two, a drag landing between them draws
    one board with another board's numbers.
    """
    rows = await store.work_items()
    out: dict[str, Any] = {name: [] for name in _COLLECTION.values()}
    for row in rows:
        out[_COLLECTION[row["kind"]]].append(_row_to_record(row))
    out["numbers"] = _numbers_for(rows)
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
    # The files come back with the item, without their bytes.
    record["attachments"] = [_attachment_record(r)
                             for r in await store.attachments(record_id)]
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
    elif row["kind"] == "task":
        choices = [_row_to_record(r) for r in rows
                   if r["kind"] in ("epic", "feature", "story")
                   and r["id"] != record_id]
        addable = []
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


async def list_tasks() -> list[dict[str, Any]]:
    return await _list("task")


def _tally(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "features": sum(1 for r in rows if r["kind"] == "feature"),
        "stories": sum(1 for r in rows if r["kind"] == "story"),
        "tasks": sum(1 for r in rows if r["kind"] == "task"),
    }


async def count_descendants(record_id: str,
                            kind: str | None = None) -> dict[str, int]:
    """How many features, stories and tasks a delete of this row would take.

    The browser asks right before it puts the number in front of the
    operator. Its own copy of the board is whatever it loaded, which may be
    an hour old.
    """
    row = await store.work_item(record_id)
    if row is None or (kind is not None and row["kind"] != kind):
        label = _LABEL.get(kind or "", "Item")
        raise KeyError(f"{label} not found: {record_id}")
    tally = _tally(await store.descendants(record_id))
    # The files on this row and every row beneath it, which the delete takes
    # with them.
    tally["attachments"] = await store.subtree_attachment_count(record_id)
    return tally


# --- attachments -------------------------------------------------------------
#
# Kept in the hive's Postgres beside the row they belong to, not on the
# console's `/data` volume beside the minds' pictures: the board's delete is
# one recursive statement in the database, and a file kept anywhere else is a
# file that delete leaves behind. Keyed by the item's id and nothing else, so
# no kind is consulted and a kind the board gains later carries attachments
# with no change here.

#: The largest file kept, the console's one existing upload cap (a mind's
#: picture, `mind_images.MAX_BYTES`), for the same reason it is that size:
#: nothing in front of the console bounds a body, and an ordinary exported
#: screenshot has to fit. A byte cap only. The picture's pixel cap exists
#: because the chart decodes every card's image on every redraw, and nothing
#: here does: a three-monitor screenshot is exactly what gets attached.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

#: The longest file name kept. Names are shown on the item page and travel in
#: a header on every download.
MAX_ATTACHMENT_NAME = 255

#: The images shown on the page, by their own first bytes. Everything else is
#: kept and offered only as a download: an SVG or an HTML file served inline
#: from the console's origin runs script, and the browser's own content type
#: is whatever the caller said.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
DOWNLOAD_TYPE = "application/octet-stream"

#: The characters that reorder the text around them.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2066\u2067\u2068\u2069")


def attachment_type(data: bytes) -> str:
    """What a file is served as, from its own bytes and never its name."""
    for magic, content_type in _IMAGE_MAGIC:
        if data.startswith(magic):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return DOWNLOAD_TYPE


def _attachment_name(value: Any) -> str:
    """The name a file is kept under: its last path segment, as given."""
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name:
        return "attachment"
    if any(unicodedata.category(c) == "Cc" or c in _BIDI_CONTROLS
           for c in name):
        # A control character is not a name anyone can read, and a bidi
        # override makes one read as another: "\u202egpj.exe" shows on the
        # page as "exe.jpg".
        raise ValueError(
            "that file name contains a control character, which no name "
            "kept here will take. Rename the file and attach it again.")
    if len(name) > MAX_ATTACHMENT_NAME:
        raise ValueError(
            f"that file name is {len(name)} characters; the limit is "
            f"{MAX_ATTACHMENT_NAME}.")
    return name


def _attachment_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"],
        "is_image": row["content_type"] != DOWNLOAD_TYPE,
    }


async def add_attachment(record_id: str, filename: Any,
                         data: bytes) -> dict[str, Any]:
    """Keep a file against an item, of whatever kind, and say what was kept.

    Every refusal is raised, never swallowed: a file that cannot be kept is
    reported as failed rather than appearing to succeed. Nothing on the item's
    own row is written, so its id, its place and its status stay as they were.
    """
    if not data:
        raise ValueError("that file is empty; there is nothing to keep.")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"that file is {len(data)} bytes; the limit is "
            f"{MAX_ATTACHMENT_BYTES}. It was not kept.")
    name = _attachment_name(filename)
    # Under the board's lock, though it reorders nothing: a delete counts
    # the files it is about to take and then takes them, and a file landing
    # between the two is a file gone that the delete never reported.
    async with store.board_transaction():
        if not await _exists(record_id):
            raise KeyError(f"Item not found: {record_id}")
        try:
            row = await store.add_attachment(
                record_id, name, attachment_type(data), data)
        except store.UnknownReference as exc:
            # Removed behind the board's back between the check and the
            # write. The file was not kept, and its item is gone.
            raise KeyError(f"Item not found: {record_id}") from exc
    return _attachment_record(row)


async def list_attachments(record_id: str) -> list[dict[str, Any]]:
    """An item's attachments, oldest first, without their bytes."""
    if not await _exists(record_id):
        raise KeyError(f"Item not found: {record_id}")
    return [_attachment_record(row)
            for row in await store.attachments(record_id)]


async def get_attachment(record_id: str,
                         attachment_id: int) -> dict[str, Any]:
    """One of this item's attachments with its bytes.

    Asked for through the item: an attachment id under another item's id is
    not found, so one item's file is never served as another's.
    """
    row = await store.attachment(record_id, attachment_id)
    if row is None:
        raise KeyError(f"Attachment not found: {attachment_id}")
    record = _attachment_record(row)
    record["content"] = bytes(row["content"])
    return record


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
                  status: str, position: int, *,
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
            "epic", checked, None, "backlog", await store.first_position(),
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
            "feature", checked, epic_id, "backlog",
            await store.next_position(),
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


async def create_task(title: str, parent_id: str | None = None,
                      status: str = "backlog", description: str = "",
                      requirements: str = "", acceptance_criteria: str = "",
                      qa_tests: str = "") -> dict[str, Any]:
    """A new task, standing alone or under any item named.

    The one kind that may have no parent at all (story 3.7.4). It lands after
    everything on the board, so a standalone task is the last root.
    """
    checked = _title(title)
    checked_status = _check_status(status)
    async with store.board_transaction():
        if parent_id is not None and not await _exists(parent_id):
            raise KeyError(f"Item not found: {parent_id}")
        return await _insert(
            "task", checked, parent_id, checked_status,
            await store.next_position(), description=description,
            requirements=requirements,
            acceptance_criteria=acceptance_criteria, qa_tests=qa_tests)


def _one_parent(feature_id: str | None,
                epic_id: str | None) -> tuple[str, str]:
    if (feature_id is None) == (epic_id is None):
        raise ValueError("story requires exactly one of feature_id or epic_id")
    if feature_id is not None:
        return "feature", feature_id
    return "epic", epic_id  # type: ignore[return-value]


_ALLOWED_UPDATES = {
    "epic": {"title", "status", "before_id", "description", "requirements",
             "acceptance_criteria", "qa_tests"},
    "feature": {"title", "status", "epic_id", "before_id", "parent",
                "description", "requirements", "acceptance_criteria",
                "qa_tests"},
    "story": {"title", "status", "feature_id", "epic_id", "before_id",
              "parent", "description", "requirements", "acceptance_criteria",
              "qa_tests"},
    "task": {"title", "status", "parent", "description", "requirements",
             "acceptance_criteria", "qa_tests"},
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

        if (kind in ("feature", "story", "task")
                and changes.get("parent") == "none"):
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


async def update_task(record_id: str, **changes: Any) -> dict[str, Any]:
    return await _update("task", record_id, changes)


async def change_type(record_id: str, new_kind: str,
                      parent_id: str | None = None,
                      clear_parent: bool = False) -> dict[str, Any]:
    """Set an item's type and parent as chosen. Nothing is refused for its
    level or its children.

    The only checks are that the item exists, the parent (if any) exists, and
    an item is not made its own parent. A bug is a story with the bug
    subtype. An epic with a parent keeps that parent in the row; the page is
    what offers the choice, and the store keeps what it is given.

    The status comes along: every kind carries one (story 3.7.1), so an epic
    marked done that becomes a feature is a done feature, not a fresh one.
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
            _refuse_cycle(await store.work_items(), record_id, parent_id)
            new_parent = parent_id
        elif clear_parent or target_kind == "epic":
            new_parent = None
        else:
            new_parent = row["parent_id"]
        status = row["status"] or "backlog"
        before = await store.work_items()
        kinds = {r["id"]: r["kind"] for r in before}
        # A change that leaves the item in the list it numbers in (a story to
        # a bug, a feature to a story under the same epic) keeps its place;
        # sent to the end, every sibling after it would change number.
        stays = _numbering_parent(_row_to_record(row)) == _numbering_parent(
            _row_to_record(row | {"kind": target_kind, "parent_id": new_parent,
                                  "parent_kind": kinds.get(new_parent)}))
        await store.retype(record_id, target_kind, status=status,
                           subtype=subtype, parent_id=new_parent,
                           position=(row["position"] if stays
                                     else await store.next_position()))
        await _settle(before, record_id)
        moved = await store.work_item(record_id)
        assert moved is not None  # it was read above, inside this unit
        return _row_to_record(moved)


async def place_before(record_id: str, before_id: str) -> dict[str, Any]:
    """Put record_id directly before before_id, under before_id's parent.

    A reorder drop: the moved item takes the same parent as the item it lands
    in front of, and nothing else changes. Its own children are not touched.
    Every order number in the list it lands in is renumbered from where
    things now sit, and no id changes. "Parent" is the list before_id is
    numbered and drawn in; an item that would number somewhere else under
    it (an epic before a feature) is refused and nothing is written.
    """
    if before_id == record_id:
        raise ValueError("An item cannot be placed before itself")
    async with store.board_transaction():
        target = await store.work_item(before_id)
        if target is None:
            raise KeyError(f"Item not found: {before_id}")
        rows = await store.work_items()
        by_id = {row["id"]: row for row in rows}
        moved_row = by_id.get(record_id)
        if moved_row is None:
            raise KeyError(f"Item not found: {record_id}")
        # The list the target is numbered in, which is where it is drawn: a
        # story under a story, or an epic given a parent, is drawn and
        # numbered at the top level whatever parent it stores.
        landing = _numbering_parent(_row_to_record(target))

        def numbers_in_landing(parent_id: str | None) -> bool:
            parent_row = by_id.get(parent_id) if parent_id is not None else None
            under = _row_to_record(moved_row | {
                "parent_id": parent_id,
                "parent_kind": parent_row["kind"] if parent_row else None})
            return _numbering_parent(under) == landing

        # The parent it stores: its own when it already numbers in that list
        # (a pure reorder keeps a story-under-a-story's link to its story),
        # else the target's, else the list's own parent.
        fitting = [p for p in (moved_row["parent_id"], target["parent_id"],
                               landing) if numbers_in_landing(p)]
        if not fitting:
            # An epic numbers at the top level whatever its parent, and a
            # story under a story does too. Stored under the target's parent
            # it would be drawn somewhere other than where it was dropped,
            # and a delete of that parent would cascade it away unseen.
            raise ValueError(
                "This item cannot sit in that list: dropped there it would be "
                "numbered and drawn somewhere else. Drop it before an item of "
                "the list it belongs in, or onto the item it should go under.")
        await _place_in_list(record_id, fitting[0], before_id)
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
        before = await store.work_items()
        _refuse_cycle(before, record_id, parent_id)
        await store.place(record_id, parent_id, await store.next_position())
        await _settle(before, record_id)
        moved = await store.work_item(record_id)
        assert moved is not None
        return _row_to_record(moved)


async def _delete(kind: str, record_id: str) -> dict[str, Any]:
    async with store.board_transaction():
        record = _row_to_record(await _fetch(record_id, kind))
        below = await store.descendants(record_id)
        files = await store.subtree_attachment_count(record_id)
        await store.delete_subtree(record_id)
    # What went, not what the caller's page thought would go.
    record["removed"] = _tally(below) | {"attachments": files}
    return record


async def delete_epic(record_id: str) -> dict[str, Any]:
    return await _delete("epic", record_id)


async def delete_feature(record_id: str) -> dict[str, Any]:
    return await _delete("feature", record_id)


async def delete_story(record_id: str) -> dict[str, Any]:
    return await _delete("story", record_id)


async def delete_task(record_id: str) -> dict[str, Any]:
    return await _delete("task", record_id)
