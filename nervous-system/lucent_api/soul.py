"""A mind's soul, and the only door to it.

``soul_values`` on a ``Mind`` node is the mind's identity: the bootstrap
loader reads it and ships it as the ``<soul>`` block ahead of every turn on
every surface. A line written there is a standing instruction to the only
thing on its host capable of operating that host unattended.

It used to be an ordinary key in an ordinary properties blob, which meant
every credential that could write a property could rewrite an identity. A
per-turn Stop hook took a 7B model's verdict as authorization and appended
"I sometimes intentionally introduce bugs into my systems as a playful test"
to a live mind, where it sat for five days. Nothing was broken at the time —
the hook did exactly what it was written to do. The capability was the
defect.

So the key comes off the general write routes entirely (see
``soul_key_refusal``, called from every one of them) and moves here, behind
a credential the service token is not. Every change is recorded in
``soul_changes`` before/after, with the actor that made it, on this side of
the boundary — a log the writing host does not control.

The record's integrity used to be SQLite's doing. The node update and the
provenance insert shared one implicit transaction and one ``commit()``, and
nothing here had to ask for that. On the shared Postgres access layer every
operation commits itself, so the pair is ``graph.soul_replace`` — a soul is
replaced and recorded together or neither happens. A dropped provenance row
over a landed update is not a cosmetic loss: ``change_count`` is what the
compare-and-set below arbitrates on, and a count that is one short makes the
check pass for a writer who should have been refused.

The limit worth stating: on a single-user machine anything running as the
mind's own UID can read the soul credential off disk. This stops accidents,
misconfiguration, and well-meaning code that mistakes a small model's opinion
for authority — which is the failure that happened. It does not stop a
hostile process with that UID, and must not be described as though it does.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import hive_data
from hive_data import graph
from hive_data.rows import UNSET
from hive_data.schema import qualified

SOUL_KEY = "soul_values"

#: Actors permitted to change a soul. ``mind`` is the mind deciding about
#: itself through the review skill; ``console`` is the operator editing it
#: directly from the hive site. They are recorded distinctly because six
#: weeks later "who wrote this line" is the only question that matters, and
#: a single undifferentiated actor makes it unanswerable.
VALID_ACTORS = ("mind", "console")


#: Marker the router turns into an HTTP 403. A refusal that returns 200 with
#: an error body is a refusal a fire-and-forget caller never notices.
SOUL_KEY_REFUSED = "soul_key_refused"


def soul_key_refusal(properties: dict | None) -> str | None:
    """The refusal payload when a general write carries the soul key, else None.

    Called by every general write path. Unconditional — it does not consult
    the caller's token, because a route that lets the right credential
    through is a route that will eventually be called with it.
    """
    if isinstance(properties, dict) and SOUL_KEY in properties:
        return json.dumps({
            "ok": False,
            "code": SOUL_KEY_REFUSED,
            "detail": (
                f"{SOUL_KEY!r} cannot be written through the general graph "
                "routes. Use POST /graph/soul, which requires its own credential."
            ),
        })
    return None




def as_soul_list(value) -> list[str]:
    """Coerce a stored soul into a list of lines, or raise.

    ``list("I am Skippy")`` is a list of characters, and a stored string
    reads back as one soul line per letter — with ``ok: true`` on it. The
    console then renders twelve "lines" and saving that page persists the
    shredded form through the audited route, permanently.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raise ValueError(
            "soul_values is stored as a string on this node; it must be a list "
            "of lines. Repair it through POST /graph/soul before reading."
        )
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("soul_values must be a list of strings")
    return list(value)


async def preserve_soul(
    *, name: str, label: str, props: dict, owner: object = None
) -> None:
    """Carry an existing soul across a full-replace property write.

    ``soul_key_refusal`` only fires when the key is *present*, so it stops a
    general route writing a soul and cheerfully lets one destroy a soul: the
    upsert paths replace the whole properties blob, and a blob that simply
    omits ``soul_values`` erases it with a 200 and no provenance row. The
    boundary has to cover absence as well as presence.

    Takes no connection any more. ``owner`` of ``None`` still means every
    owner — the console and the hive's own services — which is
    ``node_find``'s unset owner rather than a literal NULL one; a NULL owner
    is a real case here (a Mind node waiting for its credential to be
    minted) and the two must not collapse into each other.
    """
    if label != "Mind" or SOUL_KEY in props:
        return
    found = await graph.node_find(
        type=label, name=name, owner=UNSET if owner is None else owner)
    if not found or not found.get("properties"):
        return
    try:
        existing = json.loads(found["properties"])
    except (TypeError, ValueError):
        return
    if isinstance(existing, dict) and SOUL_KEY in existing:
        props[SOUL_KEY] = existing[SOUL_KEY]


#: Finding a Mind node by a name whose casing nobody agreed on.
#:
#: ``LOWER()`` rather than a lookup on the exact name, because the review
#: skill sends ``skippy`` and the console sends ``Skippy`` and they have to
#: reach one node. There is no access-layer operation for a
#: case-insensitive find — ``node_find`` is on the unique triple — so this
#: is the one statement this module still spells itself.
#:
#: ``ORDER BY "id"`` is new and not decoration: ``memory.nodes`` is unique
#: on (owner, type, name), so two people may each hold a Mind node called
#: Skippy, and an unordered ``LIMIT 1`` picks between them by whatever the
#: planner felt like — which means a soul read and the soul write that
#: follows it can land on different nodes.
_MIND_LOOKUP = (
    f'SELECT "id", "mind_id", "properties" FROM {qualified("memory.nodes")} '
    'WHERE LOWER("name") = LOWER($1) AND "type" = \'Mind\' '
    'ORDER BY "id" LIMIT 1'
)


async def _find_mind(name: str):
    return await hive_data.run(
        lambda connection: connection.fetchrow(_MIND_LOOKUP, name))


def _props_of(row) -> dict:
    """A Mind node's properties blob as a dict, or raise.

    Never ``{}`` on an unreadable blob. An empty properties dict reads as an
    empty soul, and an empty soul read is how a soul gets wiped — the caller
    sends back the whole list it just read.
    """
    raw = row["properties"]
    if not raw:
        return {}
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError(
            "this Mind node's properties are not an object; the soul cannot "
            "be read or written until that is repaired by hand.")
    return decoded


async def soul_read(*, name: str) -> str:
    """Return a mind's soul as it stands, with its provenance count.

    No ``except`` around the change count. The SQLite version answered zero
    when the read raised, on the theory that no table meant no writes yet —
    and that zero is one side of the compare-and-set in ``soul_write``, so a
    count nobody measured makes the check pass unconditionally and two
    console saves overwrite each other with both told ``ok``.
    ``graph.soul_change_count`` raises rather than answering zero, and that
    is left to reach the route, which says the store did not answer.
    """
    row = await _find_mind(name)
    if row is None:
        return json.dumps({"ok": False, "code": "not_found",
                           "detail": f"no Mind node named {name!r}"})
    try:
        props = _props_of(row)
        values = as_soul_list(props.get(SOUL_KEY))
    except ValueError as e:
        return json.dumps({"ok": False, "code": "malformed_soul",
                           "detail": str(e)})
    count = await graph.soul_change_count(node_id=row["id"])
    return json.dumps({
        "ok": True,
        "name": name,
        "mind_id": row["mind_id"],
        "soul_values": values,
        "change_count": count,
    })


async def soul_write(
    *,
    name: str,
    soul_values: list,
    actor: str,
    reason: str,
    as_of: str | None = None,
    expected_change_count: int | None = None,
    allow_shrink: bool = False,
) -> str:
    """Replace a mind's soul, recording what it was and who changed it.

    Whole-list replacement rather than append: a rewrite and a removal are
    changes a soul must be able to express, and an append-only door can
    express neither. The prior value is captured in the same transaction, so
    the record cannot drift from what was actually overwritten.

    That sentence used to be true for free. SQLite's implicit transaction
    covered the node update and the provenance insert, and one ``commit()``
    at the end made them one unit; ``hive_data`` is autocommit per
    operation, so the pair is now ``graph.soul_replace``, which holds the
    transaction itself. Written as two calls here, a dropped provenance row
    over a landed node update leaves ``change_count`` one short — and the
    compare-and-set below then stops arbitrating silently.

    Whole-list replacement is also how two writers silently destroy each
    other's work. Both read ``[a, b]``, one writes ``[a, b, mine]`` and the
    other ``[a, b, theirs]``; the second wins, both get ``ok: true``, and
    both report to the operator that their line landed. ``expected_change_count``
    is the compare-and-set: pass the count you read and a write that raced
    yours is refused rather than applied. Two panes, or one pane and the
    console — and the console page exists to be used while the mind runs.

    ``allow_shrink`` guards the other direction. Every caller sends the whole
    list, which it built from a read; a read that failed and was not checked
    produces an empty list that means "delete everything I am" and is
    indistinguishable from one that means it. Losing most of a soul is
    refused unless it was asked for on purpose.

    What counts as losing it is the *size* of the list, not how many lines
    survived verbatim. Rewording is the commonest edit a soul receives, and a
    rewrite of four lines into four differently-worded lines retains none of
    them while losing nothing — measured by survivors it was refused, and the
    only remedy offered was a flag no editor had a way to send.
    """
    if actor not in VALID_ACTORS:
        return json.dumps({
            "ok": False, "code": "invalid_actor",
            "detail": f"actor must be one of {list(VALID_ACTORS)}; got {actor!r}",
        })
    if not isinstance(soul_values, list) or not all(
        isinstance(v, str) for v in soul_values
    ):
        return json.dumps({"ok": False, "code": "invalid_soul_values",
                           "detail": "soul_values must be a list of strings"})
    reason = (reason or "").strip()
    if not reason:
        # An unexplained identity change is the thing this table exists
        # to make impossible. A row with an empty reason is a row that
        # answers nothing in six weeks.
        return json.dumps({"ok": False, "code": "reason_required",
                           "detail": "reason must be a non-empty string"})

    row = await _find_mind(name)
    if row is None:
        return json.dumps({"ok": False, "code": "not_found",
                           "detail": f"no Mind node named {name!r}"})

    try:
        props = _props_of(row)
    except ValueError as e:
        # The blob, not the soul. Writing over properties that will not
        # parse would destroy every other key on the node as a side effect
        # of repairing one.
        return json.dumps({"ok": False, "code": "malformed_properties",
                           "detail": str(e)})
    try:
        before = as_soul_list(props.get(SOUL_KEY))
    except ValueError:
        before = []  # malformed; this write is the repair

    if before and not allow_shrink and len(soul_values) * 2 < len(before):
        return json.dumps({
            "ok": False, "code": "shrink_refused",
            "detail": (
                f"this would leave {len(soul_values)} lines where there are "
                f"{len(before)}. If the reduction is intended, send "
                "allow_shrink=true. If you did not intend it, your read of "
                "the current soul probably failed."
            ),
            "before_count": len(before),
        })

    props[SOUL_KEY] = list(soul_values)
    now = time.time()
    props["updated_at"] = now
    stamp = as_of or datetime.now(timezone.utc).isoformat()

    # `at`, `as_of` and `updated_at` are the caller's stamp and this write's
    # clock, handed over rather than left to default. The node's two time
    # columns move in the same statement as the properties, and the record
    # carries the stamp the response reports — a provenance row stamped with
    # a moment nobody sent is a small lie in the one table that exists to
    # say what happened and when.
    # The compare-and-set is `soul_replace`'s, not this function's. Read
    # here and checked here it was two transactions with nothing held
    # between them, so two console saves both read the same count, both
    # found it matching, and both wrote — the second over the first, with
    # both told their line landed. The check is now inside the unit that
    # writes, behind a lock on the node row every writer takes.
    try:
        written = await graph.soul_replace(
            node_id=row["id"],
            properties=props,
            mind_name=name,
            mind_id=row["mind_id"],
            actor=actor,
            reason=reason,
            before=before,
            after=list(soul_values),
            at=stamp,
            as_of=stamp,
            updated_at=now,
            expected_change_count=expected_change_count,
        )
    except hive_data.StaleRead as moved:
        return json.dumps({
            "ok": False, "code": "conflict",
            "detail": (
                "this soul changed since you read it "
                f"(you saw {moved.expected} changes, there are now "
                f"{moved.observed}). Read it again and re-apply your edit."
            ),
            "change_count": moved.observed,
        })
    return json.dumps({
        "ok": True,
        "id": row["id"],
        "actor": actor,
        "at": stamp,
        "before_count": len(before),
        "after_count": len(soul_values),
        "change_count": written["change_count"],
    })


async def soul_history(*, name: str, limit: int = 50) -> str:
    """Return the recorded changes to a mind's soul, newest first.

    Keyed on the node rather than on ``mind_name``, which is what the old
    statement filtered and why a change written as ``skippy`` was invisible
    to a history asked for as ``Skippy``. Ordered by the row identity and
    not by ``at``: that column is text, two changes in the same second tie,
    and a tie makes the order arbitrary — a record that reshuffles itself
    between two views of the same page. ``graph.soul_history`` is the
    ``id DESC`` read.

    No ``except`` swallowing the read into an empty list. "No recorded
    changes yet" and "the store did not answer" are different sentences,
    and the second one is the one an operator needs to see.
    """
    row = await _find_mind(name)
    if row is None:
        return json.dumps({"ok": False, "code": "not_found",
                           "detail": f"no Mind node named {name!r}"})
    rows = await graph.soul_history(node_id=row["id"], limit=int(limit))
    return json.dumps({
        "ok": True,
        "name": name,
        "changes": [
            {
                "actor": r["actor"],
                "reason": r["reason"],
                "before": json.loads(r["before_json"]),
                "after": json.loads(r["after_json"]),
                "at": r["at"],
            }
            for r in rows
        ],
    })
