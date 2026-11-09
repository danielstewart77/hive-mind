"""Design sessions: the three-page specification surface a Mind fills in.

A design session is the durable record of one piece of design work — the
requirements as the Mind understands them, the tests it proposes against
named functions, and the call graph of what it intends to change. Daniel
edits the text and asks questions in place; the Mind answers by publishing
a revision.

It outlives the conversation that created it. The conversation is a pointer
on the record, not its owner, so a rotation at two in the morning leaves the
work intact and the next claim re-points it.

Three rules shape everything here.

**Items are addressed by a stable id, never by their number.** Inserting a
requirement between two others must not renumber the ones below it: the
numbers are a display order, and feedback keyed to a number would silently
reattach itself to a different requirement the moment one was inserted.

**A revision may only change what Daniel touched, and only on page one.**
He reads page one as an agreement, so a Mind quietly rewording a
requirement he already accepted is the one failure that makes the whole
surface untrustworthy. It scopes to page one because his edit to a
requirement is exactly what marks the later pages stale, and those rows
carry no feedback of their own — enforcing it there would freeze a stale
page with no way back.

No SQL lives here. Every read and write goes through `store`, which answers
from the hive's Postgres, so the three rules below are decided once, here.

**A refusal is loud.** A publish that tries to rewrite a protected item, or
a save carrying a revision that has since moved, fails and says which items
or which revision. Silently keeping the older value and reporting success
is how two browser tabs quietly delete each other's work.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import store

PAGES = ("requirements", "tests", "functions")
NOTICE_KINDS = ("feedback", "build", "accept")

# The fields on an item the Mind owns. Everything else belongs to Daniel and
# is never written by a publish.
_MIND_FIELDS: dict[str, tuple[str, ...]] = {
    "requirements": ("order", "text", "answer"),
    "tests": ("order", "requirement", "symbol", "cases", "sentence", "answer"),
    # Named to match what `design_graph.trim_call_graph` emits. A field the
    # trim produces and this tuple omits is dropped on publish and the page
    # renders blank — which is the whole of page three, silently.
    "functions": (
        "order",
        "name",
        "file",
        "repo",
        "changed",
        "role",
        # The body being proposed — a new function whole, an existing one as
        # a diff of the edit. Without it page three is a list of names, and
        # approving a call graph is not the same as approving the code.
        "symbol",
        "calls",
        "called_by",
        "params",
        "args",
        "answer",
    ),
}

# The fields Daniel owns, page by page. Feedback is his everywhere.
# Requirement wording is his because correcting it is the point of page one,
# and a test's behaviour sentence for the same reason. A function node is the
# Mind's report of what it intends to do, so there is nothing to edit there
# but the question box.
_DANIEL_FIELDS: dict[str, tuple[str, ...]] = {
    "requirements": ("text", "feedback"),
    "tests": ("sentence", "feedback"),
    "functions": ("feedback",),
}


class DesignError(Exception):
    """A design session request that cannot be satisfied as asked."""


class DesignNotFound(DesignError):
    """No design session, or no notice, with that id."""


class DesignConflict(DesignError):
    """The record moved since the caller last read it."""


class DesignUnknownReference(DesignError):
    """A design session named a mind or a conversation that does not exist.

    Its own refusal because the one database has real foreign keys where the
    split files could not: `design_sessions.mind_id` references the broker's
    minds and `conversation_id` references the gateway's sessions. A mind
    publishing its short name where its UUID belongs, or naming a
    conversation that has since been swept, used to store a row that pointed
    nowhere — it is refused now, and the refusal has to read as a bad
    reference rather than as a fault in the console.
    """


class DesignRefused(DesignError):
    """A publish tried to rewrite items Daniel did not touch."""

    def __init__(self, keys: list[str]):
        self.keys = sorted(keys)
        super().__init__(
            "Refusing to change items with no feedback: " + ", ".join(self.keys)
        )


def _unknown_reference(mind_id: str | None,
                       conversation_id: str | None) -> str:
    """Which of the two names the store would not accept, said as both.

    Both, rather than one guessed from the error: the refusal names a
    constraint and the caller passed up to two references, so saying "one of
    these two is not a row" is the whole of what is known.
    """
    named = [f"mind {mind_id!r}" for mind_id in (mind_id,) if mind_id]
    named += [f"conversation {conversation_id!r}"
              for conversation_id in (conversation_id,) if conversation_id]
    which = " or ".join(named) if named else "a reference"
    return (f"{which} is not a row this hive holds. A Mind is named by its "
            "UUID rather than its short name, and a conversation that has "
            "been swept cannot be attached to.")


def _now() -> float:
    return time.time()


def _check_page(page: str) -> str:
    if page not in PAGES:
        raise DesignError(f"Unknown page: {page}")
    return page


def new_item_id() -> str:
    """A stable key for one item, unrelated to its position on the page."""
    return uuid.uuid4().hex[:12]


def _row_to_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "conversation_id": row["conversation_id"],
        "surface": row["surface"],
        "mind_id": row["mind_id"],
        "repos": json.loads(row["repos"]),
        "requirements": json.loads(row["requirements"]),
        "tests": json.loads(row["tests"]),
        "functions": json.loads(row["functions"]),
        "accepted": json.loads(row["accepted"]),
        "stale": json.loads(row["stale"]),
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def ordered_items(record: dict[str, Any], page: str) -> list[dict[str, Any]]:
    """One page's items in display order, each carrying its own key.

    Display order is a field, so that inserting an item between two others
    is a change to three `order` values rather than a rekeying of the page.
    """
    _check_page(page)
    items = record[page]
    rows = [dict(item, key=key) for key, item in items.items()]
    rows.sort(key=lambda item: (item.get("order", 0), item["key"]))
    return rows


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def create_design_session(
    title: str,
    *,
    conversation_id: str | None = None,
    surface: str | None = None,
    mind_id: str | None = None,
    repos: list[str] | None = None,
) -> dict[str, Any]:
    """Open a design session, optionally already attached to a conversation.

    The five page columns take the schema's own defaults, so a session is
    created with five empty objects without this restating what they are.
    """
    clean_title = (title or "").strip()
    if not clean_title:
        raise DesignError("A design session needs a title")
    design_id = str(uuid.uuid4())
    now = _now()
    try:
        await store.create_design_session(
            design_id, clean_title, now, now,
            conversation_id=conversation_id, surface=surface,
            mind_id=mind_id, repos=json.dumps(list(repos or [])))
    except store.UnknownReference as exc:
        raise DesignUnknownReference(_unknown_reference(
            mind_id, conversation_id)) from exc
    return await get_design_session(design_id)


async def list_design_sessions() -> list[dict[str, Any]]:
    """Every design session, most recently written first.

    Ordered by `updated_at`, which only a write moves. Opening a session to
    read it leaves it where it was, so the top of the list stays a record of
    what is actually in flight rather than of what was looked at last.
    """
    return [_row_to_record(row) for row in await store.design_sessions()]


async def get_design_session(design_id: str) -> dict[str, Any]:
    row = await store.design_session(design_id)
    if row is None:
        raise DesignNotFound(f"No design session {design_id}")
    return _row_to_record(row)


async def delete_design_session(design_id: str) -> None:
    """Remove a design session. An id the store does not hold is an error.

    Reporting success for an id that was never there tells a caller its
    delete landed somewhere, which is the one thing it did not do.
    """
    if await store.delete_design_session(design_id) == 0:
        raise DesignNotFound(f"No design session {design_id}")


async def attach_design_session(
    design_id: str,
    conversation_id: str | None,
    *,
    surface: str | None = None,
    mind_id: str | None = None,
) -> dict[str, Any]:
    """Point an existing design session at a conversation.

    Claiming from a second conversation re-points the same record, and
    nothing on the three pages is touched: the work is the thing, the
    conversation is only where it is currently being discussed. A chat
    rotation mints a new session id and closes the old one, so without this
    every later submission would queue against a tombstone while the page
    went on displaying a conversation that looked perfectly healthy.

    The revision is deliberately not bumped. A mind re-claims on every turn
    that touches a design session, and a bump would invalidate whatever
    Daniel was halfway through typing each time it did.
    """
    await get_design_session(design_id)
    try:
        await store.attach_design_session(
            design_id, conversation_id, _now(), surface=surface,
            mind_id=mind_id)
    except store.UnknownReference as exc:
        raise DesignUnknownReference(_unknown_reference(
            mind_id, conversation_id)) from exc
    return await get_design_session(design_id)


# ---------------------------------------------------------------------------
# Publishing (the Mind) and submitting (Daniel)
# ---------------------------------------------------------------------------


def _same_text(left: Any, right: Any) -> bool:
    """Whether two pieces of prose are the same to a reader.

    A browser hands back what a textarea holds, and a textarea normalises
    CRLF and can drop a leading newline. Compared raw, any stored text with
    Windows line endings reads as edited on every single submit, and the
    Mind is told Daniel rewrote the whole page each time.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return left == right
    return left.replace("\r\n", "\n").strip("\n") == right.replace("\r\n", "\n").strip("\n")


def _touched(item: dict[str, Any]) -> bool:
    """Whether Daniel has flagged this item for the Mind to revise.

    Two ways count: a question in the box, or an edit to the text itself.
    They are separate acts — he can retype a requirement without asking
    anything about it — and treating a silent correction as untouched would
    have the next revision hand his wording straight back to him.

    The edit flag is set at submit time rather than inferred here, because a
    Mind publishing a revision that happens to match what Daniel typed would
    otherwise clear the flag on an item he is still waiting to hear about.
    """
    return bool(str(item.get("feedback") or "").strip()) or bool(item.get("edited"))


def _check_revision(record: dict[str, Any], revision: int | None) -> None:
    if revision is None:
        raise DesignConflict("This write must carry the revision it was read at")
    if int(revision) != int(record["revision"]):
        raise DesignConflict(
            f"Design session moved to revision {record['revision']};"
            f" this write was made against {revision}"
        )


async def publish_page(
    design_id: str,
    page: str,
    items: dict[str, dict[str, Any]],
    *,
    revision: int | None = None,
) -> dict[str, Any]:
    """Write the Mind's version of a page, and clear Daniel's feedback.

    One write: the Mind's values land and the questions they answer are
    cleared together, so the page can never show an answered question still
    sitting in an open box waiting for a reply.

    The question itself is kept, moved onto the item as `question_answered`
    beside the answer. Clearing it outright would leave answers floating next
    to empty boxes with nothing to say what each one was answering.

    On page one a revision may only change items Daniel touched, and an
    attempt to change any other is refused by name rather than silently
    dropped — a publish reporting success while keeping the older text is how
    a Mind and a browser tab quietly overwrite each other.
    """
    _check_page(page)
    record = await get_design_session(design_id)
    _check_revision(record, revision)
    existing: dict[str, dict[str, Any]] = record[page]
    mind_fields = _MIND_FIELDS[page]
    protected = page == "requirements" and bool(existing)

    refused: list[str] = []
    merged: dict[str, dict[str, Any]] = {}

    for key, current in existing.items():
        item = dict(current)
        incoming = items.get(key)
        may_change = (not protected) or _touched(current)
        if incoming is None:
            # Omitted. On a protected page that keeps the item: a Mind
            # publishing a partial page — reconciling ids badly, or leaving
            # out what it thinks it already answered — must not be able to
            # delete a requirement, and least of all the flagged one Daniel
            # is still arguing about. Elsewhere the page is the Mind's to
            # rewrite wholesale.
            if not protected:
                continue
            merged[key] = item
            continue
        if incoming.get("deleted"):
            # Deletion is a thing the Mind says, not a thing it omits, and
            # on page one it still needs Daniel to have flagged the item.
            if not protected or may_change:
                continue
            refused.append(key)
            merged[key] = item
            continue
        changes = {
            field: incoming[field]
            for field in mind_fields
            if field in incoming and incoming[field] != current.get(field)
        }
        # Position is not content. Splitting one requirement in two moves
        # every sibling below it, and refusing that would make inserting a
        # requirement impossible — which is precisely what the separate
        # `order` field exists to allow.
        reorder = changes.pop("order", None)
        if reorder is not None:
            item["order"] = reorder
        if changes and not may_change:
            refused.append(key)
            merged[key] = item
            continue
        item.update(changes)
        merged[key] = item

    for key, incoming in items.items():
        if key in existing:
            continue
        item = {field: incoming[field] for field in mind_fields if field in incoming}
        merged[key] = item

    if refused:
        raise DesignRefused(refused)

    for item in merged.values():
        question = str(item.get("feedback") or "").strip()
        if question:
            item["question_answered"] = question
        item["feedback"] = ""
        item["edited"] = False

    accepted = dict(record["accepted"])
    accepted[page] = False
    stale = dict(record["stale"])
    if page == "requirements":
        stale.update(_downstream_stale(record["requirements"], merged, record))
    else:
        stale[page] = False

    return await _write_page(
        design_id, page, merged, accepted=accepted, stale=stale,
        expected_revision=record["revision"],
    )


async def submit_page(
    design_id: str,
    page: str,
    *,
    items: dict[str, dict[str, Any]],
    action: str = "feedback",
    revision: int | None = None,
) -> dict[str, Any]:
    """Write Daniel's edits and questions, and queue a notice for the Mind.

    The notice is written in the same transaction as the edits, so a Mind
    that is unreachable, restarting, or attached to a conversation that has
    since rotated loses nothing: the work is on the record before anyone
    tries to deliver anything.
    """
    _check_page(page)
    if action not in NOTICE_KINDS:
        raise DesignError(f"Unknown action: {action}")
    if action == "build" and page != "functions":
        raise DesignError("Build is only offered on the functions page")
    record = await get_design_session(design_id)
    _check_revision(record, revision)
    if action == "build":
        if not record["conversation_id"]:
            raise DesignError(
                "This design session is not attached to a conversation, so there"
                " is nobody to build it"
            )
        # Build is the one press that starts a harness writing code, so it
        # consults every gate rather than only the one on the page it was
        # pressed from. A disabled button is a courtesy; this is the check.
        for gate_page in PAGES:
            allowed, reason = await can_advance(design_id, gate_page)
            if not allowed:
                raise DesignError(f"{gate_page}: {reason}")
            if record["stale"].get(gate_page):
                raise DesignError(
                    f"{gate_page} was written against older requirements"
                )
            # Pages one and two are accepted explicitly; page three is
            # accepted by this press, which is what Build means.
            if gate_page != "functions" and not record["accepted"].get(gate_page):
                raise DesignError(f"{gate_page} has not been accepted yet")
    existing: dict[str, dict[str, Any]] = record[page]
    editable = _DANIEL_FIELDS[page]

    merged: dict[str, dict[str, Any]] = {}
    for key, current in existing.items():
        item = dict(current)
        incoming = items.get(key) or {}
        for field in editable:
            if field in incoming and not _same_text(incoming[field], current.get(field)):
                item[field] = incoming[field]
                if field != "feedback":
                    item["edited"] = True
        merged[key] = item

    accepted = dict(record["accepted"])
    accepted[page] = False
    stale = dict(record["stale"])
    if page == "requirements":
        stale.update(_downstream_stale(record["requirements"], merged, record))

    return await _write_page(
        design_id, page, merged, accepted=accepted, stale=stale,
        expected_revision=record["revision"], notice=(action, page),
    )


async def accept_page(
    design_id: str, page: str, *, revision: int | None = None
) -> dict[str, Any]:
    """Record that Daniel has accepted a page as it stands.

    Acceptance is state, not navigation: the tests page's gate reads it, and
    a later submission on the same page clears it, so a page cannot stay
    accepted while carrying feedback nobody has answered.

    It also queues a notice, for the same reason a submission does. An
    acceptance the Mind is never told about is a design session stalled at
    an empty next page with both parties waiting on the other — Daniel has
    signed the page off and moved on, and the only party who can write the
    next one does not know it happened.
    """
    _check_page(page)
    record = await get_design_session(design_id)
    _check_revision(record, revision)
    allowed, reason = await can_advance(design_id, page)
    if not allowed:
        raise DesignError(reason)
    if record["stale"].get(page):
        raise DesignError(
            "This page was written against older requirements — it has to be"
            " republished before it can be accepted"
        )
    accepted = dict(record["accepted"])
    accepted[page] = True
    return await _write_page(
        design_id, page, record[page], accepted=accepted,
        stale=record["stale"],
        expected_revision=record["revision"], notice=("accept", page),
    )


def _downstream_stale(
    before_items: dict[str, dict[str, Any]],
    after_items: dict[str, dict[str, Any]],
    record: dict[str, Any],
) -> dict[str, bool]:
    """Mark the later pages stale when requirement wording actually moved.

    Keyed on the text changing rather than on a write happening: a submit
    that only asks a question has changed no requirement, and flagging the
    tests page stale for that would teach the reader to ignore the flag.
    """
    before = {key: item.get("text") for key, item in before_items.items()}
    after = {key: item.get("text") for key, item in after_items.items()}
    if before == after:
        return {}
    return {page: True for page in ("tests", "functions") if record[page]}


async def _write_page(
    design_id: str,
    page: str,
    items: dict[str, dict[str, Any]],
    *,
    accepted: dict[str, Any],
    stale: dict[str, Any],
    expected_revision: int,
    notice: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Write one page, but only if the record is still where it was read.

    The revision travels into the UPDATE itself rather than being compared
    in Python beforehand. A read, a check and a write across three
    connections leaves a window wide enough for two browser tabs to both
    pass the check and both write, and the second one wins silently — which
    is the exact failure the revision exists to prevent.

    A notice is queued inside the same unit of work, so a submission cannot
    end up stored with nobody ever told about it, or a notice orphaned
    against a session that was deleted between the two writes.
    """
    now = _now()
    async with store.transaction():
        written = await store.write_design_page(
            design_id, page, json.dumps(items), json.dumps(accepted),
            json.dumps(stale), expected_revision, now)
        if written == 0:
            row = await store.design_session(design_id)
            if row is None:
                raise DesignNotFound(f"No design session {design_id}")
            raise DesignConflict(
                f"Design session moved to revision {row['revision']};"
                f" this write was made against {expected_revision}"
            )
        if notice is not None:
            kind, notice_page = notice
            await store.queue_notice(design_id, kind, notice_page, now)
    return await get_design_session(design_id)


async def can_advance(design_id: str, page: str) -> tuple[bool, str]:
    """Whether a page may be accepted, and why not when it may not.

    The tests page carries the real gate. A test naming a function that is
    neither on disk nor specified on page three is not a test yet, and
    accepting the page would carry that fiction into the build.
    """
    _check_page(page)
    record = await get_design_session(design_id)
    items = record[page]
    if not items:
        return False, "This page has nothing on it yet"
    if page == "tests":
        planned = {
            str(item["name"])
            for item in record["functions"].values()
            if str(item.get("name") or "").strip()
        }
        unresolved = []
        for key, item in items.items():
            symbol = item.get("symbol") or {}
            if symbol.get("resolved"):
                continue
            named = str(symbol.get("name") or "").strip()
            if symbol.get("kind") == "planned" and named and named in planned:
                continue
            unresolved.append(symbol.get("name") or key)
        if unresolved:
            return False, "Unresolved functions: " + ", ".join(sorted(unresolved))
        thin = []
        for key, item in items.items():
            outcomes = {
                str(case.get("outcome") or "").lower()
                for case in (item.get("cases") or [])
            }
            if not {"pass", "fail"} <= outcomes:
                thin.append(str(item.get("sentence") or key))
        if thin:
            return False, (
                "These tests need a value that should succeed and one that"
                " should fail: " + ", ".join(sorted(thin))
            )
    return True, ""


# ---------------------------------------------------------------------------
# Notices — what the delivery path claims and sends
# ---------------------------------------------------------------------------


async def _queue_notice(design_id: str, *, kind: str, page: str) -> None:
    await store.queue_notice(design_id, kind, page, _now())


async def pending_notices(design_id: str | None = None) -> list[dict[str, Any]]:
    """Undelivered notices, oldest first, with the conversation to reach.

    Joined to the session by the store rather than read in a second
    statement: the delivery path needs the conversation, the surface and the
    mind to send anything at all, and a notice whose session was deleted
    between two reads is a send with nowhere to go.
    """
    return list(await store.pending_notices(design_id))


async def claim_notice(notice_id: int) -> dict[str, Any]:
    """Take a notice for delivery, so two senders cannot both send it.

    Claiming and delivering are separate because Build is the one signal
    that starts a harness writing code: delivered twice, two processes edit
    one working tree, each unaware of the other. The claim is the guard, and
    it fails rather than returning a second copy.
    """
    row = await store.claim_notice(notice_id, _now())
    if row is None:
        raise DesignNotFound(f"Notice {notice_id} is not available to claim")
    return dict(row)


async def release_notice(notice_id: int) -> None:
    """Hand a claimed notice back when the send failed.

    Delivery is acknowledged after the fact, never assumed at hand-off: a
    transport that raised has delivered nothing, and a notice that stays
    claimed would be a submission Daniel never hears about again.
    """
    await store.release_notice(notice_id)


async def mark_delivered(notice_id: int) -> None:
    if await store.mark_notice_delivered(notice_id, _now()) == 0:
        raise DesignNotFound(f"No undelivered notice {notice_id}")
