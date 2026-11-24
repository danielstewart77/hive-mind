"""The named operations over the `staff` schema: the chief of staff's ledger.

The ledger holds only what Daniel has handed the chief of staff to watch. A
task is a name, a short description (the task itself, or a line naming the
board item it covers) and a status; the sessions working it are pairs in
`staff.task_sessions`. The chief of staff and its tick both reach the ledger
through these operations rather than writing SQL.

A status is a row of `staff.statuses`. One that is not there is refused as
`MissingReference`: by the foreign key on a write, inside the statement that
would have written it, so a refused update leaves the task exactly as it
was; and by a lookup on a read, so a misspelt filter is an error rather than
an empty list that reads as "nothing in that state".

The session in a pair is `core.sessions.id`, the hive-comms session id that
survives every rotation, never a harness conversation id. It is not a
foreign key: comms deletes a session row when its conversation ends, and the
pair outlives it. A paired session with no `core.sessions` row has ended. A
session works one task at a time: attaching a session that is already
paired is a `Conflict`, to this task or another.

Names, ids and session ids that are empty or made only of whitespace -
Unicode whitespace and the zero-width characters included - are refused as
`ValueRefused`, here and again by the schema's own checks.

Every operation takes an optional `unit` and behaves as the `core` ones do:
given one it runs inside that caller's transaction, given none it runs on
its own connection and commits by itself.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Collection

from . import _sql
from .core import _execute, _execute_returning, _fetch, _fetch_one
from .errors import MissingReference, ValueRefused
from .rows import UNSET
from .schema import COLUMNS, qualified, select_list
from .unit import Unit

Row = dict[str, Any]

STATUSES = "staff.statuses"
TASKS = "staff.tasks"
TASK_SESSIONS = "staff.task_sessions"
AGENTS = "staff.agents"

#: What `update_task` may set. `updated_at` is moved by the operation
#: itself, and `id` and `created_at` never move.
_SETTABLE = frozenset({"name", "description", "status", "board_item",
                       "question", "updated_at"})

#: What the tick may set. Disjoint from `_SETTABLE` on purpose: a status is
#: Daniel's reading of the work, and the machine that watches sessions never
#: writes one. `updated_at` is shared because a stop is a real change to
#: report, and the sitrep finds what moved by that column.
_WATCHABLE = frozenset({"stopped_at", "woken_at", "updated_at"})

#: What `finish_agent` and the sweep may set. A sub-agent's brief, task and
#: start never move: the row is a record of what ran, not a slot to be
#: reused.
_AGENT_SETTABLE = frozenset({"finished_at", "outcome"})

#: What a reported event may move on a row that already exists. The identity,
#: the mind that owns it and the moment it started are what the row *is* and
#: never move; everything here is something a later event can know better
#: than the first one did - a dsh start carries no kind at all and the
#: child's `finished` event is the first thing to name its provider.
_AGENT_REPORTABLE = frozenset({
    "agent_type", "about", "session_id", "conversation_ref",
    "parent_agent_id", "active_at", "finished_at", "outcome", "task_id"})

#: An end a harness reported without saying how it went. Distinct from an
#: empty outcome, which is a row nobody has ended at all.
OUTCOME_UNKNOWN = "unknown"

#: What no reporter may send as an outcome of its own. `OUTCOME_ABANDONED`
#: is the one ending a later report is allowed to replace, so a harness that
#: could send it could have its own first ending overwritten by its own
#: second one - and a reader could not tell the sweep's guess from something
#: a harness actually said.
#: Declared below the two values and used by `record_agent_event`.
#:
#: An end nobody ever reported, written by `retire_stale_agents`. Separate
#: from `OUTCOME_UNKNOWN` on purpose: a child that finished cleanly and said
#: nothing about it, and a child that was killed with its turn, are different
#: facts, and a reader deciding whether anything went wrong needs to tell
#: them apart. It is also the one outcome a real ending is allowed to
#: replace.
OUTCOME_ABANDONED = "abandoned"

#: The three states a sub-agent record can be read as. Derived from the row
#: and a clock rather than stored: a stored state is wrong the moment the
#: child dies without writing, which is the ordinary way a child ends.
ENDED = "ended"
WORKING = "working"
RUNNING = "running"

#: What separates the mind from the harness's own id in a stored key, and is
#: refused inside either half so the composition stays injective.
AGENT_SEPARATOR = ":"

#: Outcomes the machinery writes and a reporter may not. See the comment on
#: `OUTCOME_ABANDONED`.
RESERVED_OUTCOMES = frozenset({OUTCOME_ABANDONED})

#: Characters that render as nothing but are not `str.isspace`. The schema's
#: checks carry the same set.
_INVISIBLE = frozenset("\u180e\u200b\u200c\u200d\u2060\ufeff")


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _not_blank(what: str, value: Any) -> None:
    if isinstance(value, str) and all(
            c.isspace() or c in _INVISIBLE for c in value):
        raise ValueRefused(
            f"a staff {what} of {value!r} is blank. It would render as "
            "nothing on every surface the ledger is shown on.")


# ----------------------------------------------------------------- statuses

async def statuses(*, unit: Unit | None = None) -> list[str]:
    """Every status a task may be given, by name."""
    rows = await _fetch(
        unit, f'SELECT "name" FROM {qualified(STATUSES)} ORDER BY "name"')
    return [r["name"] for r in rows]


async def add_status(name: str, *, unit: Unit | None = None) -> Row:
    """Make a status available to tasks. `Conflict` if it already is."""
    _not_blank("status", name)
    sql, args = _sql.insert(STATUSES, {"name": name})
    return await _execute(unit, sql, *args)


async def _known_status(status: str, unit: Unit | None) -> None:
    found = await _fetch_one(
        unit, f'SELECT "name" FROM {qualified(STATUSES)} WHERE "name" = $1',
        status)
    if found is None:
        raise MissingReference(
            f"{status!r} is not a staff status, so no task can be in it. "
            "An empty list here would read as nothing in that state; the "
            "statuses are staff.statuses.")


# -------------------------------------------------------------------- tasks

async def create_task(name: str, status: str, *, description: str = "",
                      task_id: str | None = None, now: float | None = None,
                      unit: Unit | None = None) -> Row:
    """Open a task and return it as stored.

    `task_id` defaults to a fresh uuid; `now` stamps both `created_at` and
    `updated_at` and defaults to the wall clock. A status not in
    `staff.statuses` is refused as `MissingReference` and nothing is written.
    """
    _not_blank("task name", name)
    if task_id is not None:
        _not_blank("task id", task_id)
    moment = _now(now)
    sql, args = _sql.insert(TASKS, {
        "id": task_id if task_id is not None else uuid.uuid4().hex,
        "name": name, "description": description, "status": status,
        "created_at": moment, "updated_at": moment,
    }, returning=COLUMNS[TASKS])
    return (await _execute_returning(unit, sql, *args)).get("row")


async def task(task_id: str, *, unit: Unit | None = None) -> Row | None:
    """One task by id, or None when there is no such task."""
    return await _fetch_one(
        unit, f"SELECT {select_list(TASKS)} FROM {qualified(TASKS)} "
        'WHERE "id" = $1', task_id)


async def tasks(*, status: str | None = None,
                unit: Unit | None = None) -> list[Row]:
    """Every task, or every task in one status; most recently moved first.

    A status that is not in `staff.statuses` is refused as
    `MissingReference` rather than answered with an empty list.
    """
    if status is None:
        where, args = "", []
    else:
        await _known_status(status, unit)
        where, args = 'WHERE "status" = $1', [status]
    return await _fetch(
        unit, f"SELECT {select_list(TASKS)} FROM {qualified(TASKS)} {where} "
        'ORDER BY "updated_at" DESC, "id"', *args)


async def update_task(task_id: str, *, name: Any = UNSET,
                      description: Any = UNSET, status: Any = UNSET,
                      board_item: Any = UNSET, question: Any = UNSET,
                      expected_status: Any = UNSET,
                      now: float | None = None,
                      unit: Unit | None = None) -> Row | None:
    """Change a task's name, description, status, board item or question.

    `updated_at` moves with any of them.

    `board_item` is the `board.work_items` id this task covers and is what
    gives the Chief of Staff page its depth: a task that names a feature
    draws that feature's stories beneath it. `question` is the one thing the
    task is waiting on Daniel for, and a task holding one draws a bubble.
    Either may be set to None, which is how an answered question is cleared;
    passing nothing leaves both as they were.

    Only what is passed is written. Returns the task as it now stands, or
    None when nothing was changed: there is no such task, or
    `expected_status` was given and the task is no longer in it.

    `expected_status` is the guard, in the statement, for a writer acting on
    a status it read earlier - the tick putting a running task on hold must
    not resurrect one Daniel closed in between. Without it the write is
    unconditional.

    A status not in `staff.statuses` is refused as `MissingReference` and
    the task is left unchanged.
    """
    if name is not UNSET:
        _not_blank("task name", name)
    values = _sql.passed(
        {"name": name, "description": description, "status": status,
         "board_item": board_item, "question": question})
    if not values:
        raise ValueRefused(
            "an update of a staff task named nothing to change. Moving "
            "updated_at alone would report the task as touched when nothing "
            "about it was.")
    values["updated_at"] = _now(now)
    extra_where, extra_args = None, []
    if expected_status is not UNSET:
        if expected_status is None:
            raise ValueRefused(
                "a task always has a status, so expecting none matches "
                "nothing and every update would report the task missing.")
        extra_where, extra_args = _sql.where(
            TASKS, {"status": expected_status}, start=len(values) + 2)
    sql, args = _sql.update(TASKS, values, {"id": task_id},
                            settable=_SETTABLE, extra_where=extra_where,
                            returning=COLUMNS[TASKS])
    return (await _execute_returning(unit, sql, *args, *extra_args)
            ).get("row")


# -------------------------------------------------------------------- watch
#
# The tick's three writes. It never touches a status: `note_stopped` and
# `note_running` record what the live feed said about a task's sessions, and
# `note_woken` records that the chief of staff has been told. A wake is
# pending while `stopped_at` is set and `woken_at` is null or older, so
# there is no queue anywhere and nothing to clear.


async def note_stopped(task_id: str, *, now: float | None = None,
                       unit: Unit | None = None) -> Row | None:
    """Record that every session on this task has stopped. First one wins.

    Guarded by `stopped_at IS NULL` in the statement, so the moment kept is
    the first tick that saw the stop rather than the latest one, and a tick
    running while another already recorded it changes nothing and reports
    None. `updated_at` moves with it: a watched session stopping is a real
    change to the task, and the sitrep reads that column to find what moved.
    """
    moment = _now(now)
    clause, clause_args = _sql.where(TASKS, {"stopped_at": None}, start=4)
    sql, args = _sql.update(
        TASKS, {"stopped_at": moment, "updated_at": moment}, {"id": task_id},
        settable=_WATCHABLE, extra_where=clause,
        returning=COLUMNS[TASKS])
    return (await _execute_returning(unit, sql, *args, *clause_args)
            ).get("row")


async def note_running(task_id: str, *,
                       unit: Unit | None = None) -> Row | None:
    """Clear the stop: a session on this task is producing output again.

    Without this a task dispatched again after it was reported would keep
    its first `stopped_at` forever and never go pending a second time.
    Clearing it also drops a stop that was never reported, which is correct:
    the news was that the work had stopped, and it has not.

    `updated_at` does not move. Work resuming is what the chief of staff
    just asked for, and the sitrep reporting it as movement would list every
    relay twice.
    """
    sql, args = _sql.update(
        TASKS, {"stopped_at": None}, {"id": task_id},
        settable=_WATCHABLE, extra_where='"stopped_at" IS NOT NULL',
        returning=COLUMNS[TASKS])
    return (await _execute_returning(unit, sql, *args)).get("row")


async def note_woken(task_id: str, *, now: float | None = None,
                     unit: Unit | None = None) -> Row | None:
    """Record that the chief of staff has been told about this task.

    `updated_at` does not move: delivering a wake is the machine's own
    bookkeeping, not a change in the work, and moving it would have every
    sitrep report the previous sitrep's notifications as news.
    """
    sql, args = _sql.update(
        TASKS, {"woken_at": _now(now)}, {"id": task_id},
        settable=_WATCHABLE, returning=COLUMNS[TASKS])
    return (await _execute_returning(unit, sql, *args)).get("row")


def wake_pending(row: Row) -> bool:
    """Whether this task is owed a wake, read from the row alone.

    One definition, used by the tick to choose and by anything else that
    wants the same answer without a second opinion about it.
    """
    stopped = row.get("stopped_at")
    if stopped is None:
        return False
    woken = row.get("woken_at")
    return woken is None or woken < stopped


async def delete_task(task_id: str, *, unit: Unit | None = None) -> Row:
    """Remove a task and its session pairs. Reports rows changed; 0 if none.

    For a task opened in error. A task that was worked is closed by moving
    it to `done`, which keeps its record; deleting it loses which sessions
    worked it.
    """
    clause, args = _sql.where(TASKS, {"id": task_id})
    return await _execute(
        unit, f"DELETE FROM {qualified(TASKS)} WHERE {clause}", *args)


# ------------------------------------------------------------------ agents
#
# The sub-agents a mind has spawned. A sub-agent is an in-process child of a
# mind's turn, so nothing outside that process can enumerate one: something
# inside the turn writes the row on the way in and closes it on the way out,
# and that row is the only record of it that outlives the turn.
#
# Two writers, one table. The chief of staff's delegate briefs register
# themselves against the ledger task they were handed (`start_agent`,
# `finish_agent`), which is what step 9 was built for. A harness reporting
# its own children knows no task at all - it knows its mind, its
# conversation, the kind of child and which child spawned it - and comes
# through `record_agent_event`, which is idempotent in every direction a
# detached reporter can get wrong. Both land in one shape, and `agent_state`
# is the single reading of what a row says the child is doing.


async def start_agent(task_id: str, brief: str, *, about: str = "",
                      agent_id: str | None = None, now: float | None = None,
                      unit: Unit | None = None) -> Row:
    """Record a sub-agent beginning work on a task, and return its row.

    `brief` is the brief file it was handed, `about` the one thing it was
    handed it for - a session id, a board number - so two children on the
    same brief are telling apart on the page. `MissingReference` when there
    is no such task, so a typo does not leave a child hanging off nothing.
    """
    _not_blank("agent brief", brief)
    if agent_id is not None:
        _not_blank("agent id", agent_id)
    sql, args = _sql.insert(AGENTS, {
        "id": agent_id if agent_id is not None else uuid.uuid4().hex,
        "task_id": task_id, "brief": brief, "about": about,
        # The brief file *is* what kind of child this is, in this writer's
        # vocabulary, so the kind field is populated for every row that has
        # a brief rather than only for rows a harness wrote. A reader asking
        # what kind of child this is never has to know which writer made it.
        "agent_type": brief,
        "started_at": _now(now),
    }, returning=COLUMNS[AGENTS])
    return (await _execute_returning(unit, sql, *args)).get("row")


async def finish_agent(agent_id: str, *, outcome: str = "",
                       now: float | None = None,
                       unit: Unit | None = None) -> Row | None:
    """Close a sub-agent's row. None when there is no such open agent.

    Guarded in the statement, so the moment kept is the first report rather
    than the latest: a brief that reports twice does not move the time its
    work actually ended.

    One ending may be replaced, and only one: the `OUTCOME_ABANDONED` the
    sweep writes for a child nobody ever closed. Without that, a sweep firing
    one tick before the real ending arrives makes the truth unwritable for
    ever - the row says abandoned, the child's own report matches no open row
    and comes back as None, and the record is wrong permanently. A reported
    outcome is never replaced by another reported outcome.
    """
    sql, args = _sql.update(
        AGENTS, {"finished_at": _now(now), "outcome": outcome},
        {"id": agent_id}, settable=_AGENT_SETTABLE,
        extra_where=f'"finished_at" IS NULL OR "outcome" = \'{OUTCOME_ABANDONED}\'',
        returning=COLUMNS[AGENTS])
    return (await _execute_returning(unit, sql, *args)).get("row")


#: The events a reporter can send about one child, and the whole of them.
#: A harness knows three things about a child and no more: that it began,
#: that it is still producing work, and that it is over.
SPAWNED = "spawned"
EVENT_WORKING = "working"
EVENT_ENDED = "ended"
_EVENTS = (SPAWNED, EVENT_WORKING, EVENT_ENDED)

#: How long after a reported ending an activity report must arrive to mean
#: the child was woken, rather than being the tail of the run that ended.
REOPEN_AFTER_SECONDS = 30.0

#: How an event lands on a row that already exists.
#:
#: Written as one statement rather than a read and a write, because the two
#: reporters of one child race by construction: a harness hook is detached
#: so that a slow recorder can never delay a spawn, which means a short
#: child's start and end are in flight at the same time and either may
#: arrive first. A statement that reads, decides and writes would lose one
#: of them.
#:
#: Bare column names here are the row as it stands; `EXCLUDED` is the event
#: that just arrived. What the row *is* - its identity, its mind, when it
#: started - never moves. Everything else is something a later event may
#: know better than the first: dsh's start carries no kind at all, and the
#: child's own ending is the first event to name its provider.
#:
#: A kind or a label already recorded is never replaced by an event that
#: carries none, which is the ordinary shape of a stop report: claude's
#: names the agent and nothing about what it was for, so taking the newest
#: value would blank what the spawn said.
#:
#: And an activity report *reopens* a record the sweep abandoned. The sweep
#: only ever guessed that the child was dead; a harness saying it is still
#: producing work is the one piece of evidence that outranks the guess, and
#: discarding it left `WORKING` unreachable for the rest of that child's
#: life.
#:
#: A *reported* ending is reopened by an activity report too, but only one
#: that arrives well after it. A harness can wake a child it already ended:
#: dsh sends a finished sub-agent a message and it works again under the
#: same id, which is how a reviewer's findings go back to the builder. On
#: 2026-11-23 a builder two rounds into review fixes was drawn as finished
#: for an hour, and Daniel watching the page saw nothing working at all. A
#: start never reopens anything, and nor does an activity report within
#: `REOPEN_AFTER_SECONDS` of the ending: the reporters are detached, and a
#: report of the last thing the child did can land a moment after the
#: report that it stopped.
_AGENT_UPSERT = f"""ON CONFLICT ("id") DO UPDATE SET
    "agent_type" = CASE WHEN "agents"."agent_type" = ''
        THEN EXCLUDED."agent_type" ELSE "agents"."agent_type" END,
    "about" = CASE WHEN "agents"."about" = ''
        THEN EXCLUDED."about" ELSE "agents"."about" END,
    "session_id" = COALESCE("agents"."session_id", EXCLUDED."session_id"),
    "conversation_ref" = COALESCE("agents"."conversation_ref",
                                  EXCLUDED."conversation_ref"),
    "parent_agent_id" = COALESCE("agents"."parent_agent_id",
                                 EXCLUDED."parent_agent_id"),
    "task_id" = COALESCE("agents"."task_id", EXCLUDED."task_id"),
    "active_at" = CASE WHEN EXCLUDED."active_at" IS NULL
        THEN "agents"."active_at"
        ELSE GREATEST(COALESCE("agents"."active_at", EXCLUDED."active_at"),
                      EXCLUDED."active_at") END,
    "finished_at" = CASE
        WHEN EXCLUDED."active_at" IS NOT NULL
             AND "agents"."outcome" = '{OUTCOME_ABANDONED}' THEN NULL
        WHEN EXCLUDED."active_at" IS NOT NULL
             AND "agents"."finished_at" IS NOT NULL
             AND EXCLUDED."active_at"
                 > "agents"."finished_at" + {REOPEN_AFTER_SECONDS} THEN NULL
        WHEN EXCLUDED."finished_at" IS NULL THEN "agents"."finished_at"
        WHEN "agents"."finished_at" IS NULL
             OR "agents"."outcome" = '{OUTCOME_ABANDONED}'
            THEN GREATEST(EXCLUDED."finished_at", "agents"."started_at")
        ELSE "agents"."finished_at" END,
    "outcome" = CASE
        WHEN EXCLUDED."active_at" IS NOT NULL
             AND "agents"."outcome" = '{OUTCOME_ABANDONED}' THEN ''
        WHEN EXCLUDED."active_at" IS NOT NULL
             AND "agents"."finished_at" IS NOT NULL
             AND EXCLUDED."active_at"
                 > "agents"."finished_at" + {REOPEN_AFTER_SECONDS} THEN ''
        WHEN EXCLUDED."finished_at" IS NULL THEN "agents"."outcome"
        WHEN "agents"."finished_at" IS NULL
             OR "agents"."outcome" = '{OUTCOME_ABANDONED}'
            THEN EXCLUDED."outcome"
        ELSE "agents"."outcome" END"""


def agent_key(mind_id: str, agent_id: str,
              conversation_ref: str | None = None) -> str:
    """The id a reported child is stored under, hive-wide.

    A harness's own agent id is unique inside the conversation that spawned
    it and nowhere else - dsh says so in as many words ("unique in the parent
    namespace") and claude mints its sixteen hex characters per conversation.
    The primary key here covers every mind in the hive, so the key has to
    carry everything the id's own uniqueness rests on.

    Both halves above it, then. Without the mind, two minds' children
    collide: the second start is refused as a duplicate and the second mind's
    ending closes the first mind's row. Without the conversation, one mind
    reusing an id in a later conversation collides with *itself*: the second
    child merges into the first child's finished record, keeps the first's
    kind and label, and the reporter is handed back a 200 describing
    somebody else's work while a reader sees no children at all in the second
    conversation. A reporter always knows which conversation it is in - that
    is its own environment rather than anything it has to read out of an
    event - so it is always available to send.

    `conversation_ref` absent means a child that belongs to no conversation
    this hive knows, and those key on the mind alone: there is nothing
    narrower to use, and such children are not drawn anywhere.

    Composed in one place rather than by each reporter, so two writers cannot
    disagree about it. The separator is refused inside every part, because
    without that the composition is not injective: a mind `m` with a child
    `a:b` and a mind `m:a` with a child `b` would compose the same key and
    merge two children into one record.
    """
    _not_blank("agent mind", mind_id)
    _not_blank("agent id", agent_id)
    parts = [("mind", mind_id)]
    if conversation_ref:
        _not_blank("agent conversation", conversation_ref)
        parts.append(("conversation", conversation_ref))
    parts.append(("id", agent_id))
    for what, value in parts:
        if AGENT_SEPARATOR in value:
            raise ValueRefused(
                f"a sub-agent {what} of {value!r} holds "
                f"{AGENT_SEPARATOR!r}, which separates the parts of the "
                "stored key. Two different sets of parts would compose the "
                "same key and merge two children into one record.")
    return AGENT_SEPARATOR.join(value for _, value in parts)


async def record_agent_event(
        agent_id: str, state: str, *, mind_id: str, agent_type: str = "",
        label: str = "", conversation_ref: str | None = None,
        session_id: str | None = None, parent_agent_id: str | None = None,
        task_id: str | None = None, outcome: str | None = None,
        now: float | None = None, unit: Unit | None = None) -> Row:
    """Record one reported event about one sub-agent, and return its row.

    The one way a harness's children reach the ledger, and idempotent in
    every direction a detached reporter can get wrong:

    * The same event twice - a retried write, a hook that fired twice -
      lands once and changes nothing the second time.
    * An ending that arrives before its own start creates the row already
      ended, and the start arriving afterwards does not reopen it.
    * A start that arrives after the sweep has retired that identity does
      not resurrect it either, because the ending is already written and an
      ending is never unwritten.

    `agent_id` is the harness's own id and is namespaced by the mind and the
    conversation before it is stored, because that is the scope its own
    uniqueness rests on; `parent_agent_id` is namespaced the same way, since
    a child's parent ran in the same process and the same conversation. `state` is one of
    `SPAWNED`, `EVENT_WORKING` or `EVENT_ENDED`.

    One limit, named rather than hidden: `started_at` is the moment this
    recorder first heard of the child. When an ending arrives for a child
    whose start report was lost or overtaken, the record it creates has its
    start and its end at the same moment, and nothing can recover the real
    duration - the start report that arrives afterwards is stamped later
    still, because the clock here is the receipt clock. Such a record is
    identifiable (its start equals its end) and is honest about knowing
    nothing rather than inventing a plausible length.

    `now` is the recorder's clock and stamps whichever moments this event
    moves. It is deliberately not the reporting host's: minds run on other
    machines and in containers, and a host running ahead would hold a child
    in the future - reading as freshly active, and beyond the sweep's reach,
    for as long as the drift lasted.

    An `EVENT_ENDED` with no `outcome` is stored as `OUTCOME_UNKNOWN`: the
    harness said the child is over and said nothing about how it went, which
    is a different fact from a child nobody ended at all.
    """
    if state not in _EVENTS:
        raise ValueRefused(
            f"{state!r} is not a sub-agent event. A reporter knows that a "
            f"child began, that it is still working, or that it is over: "
            f"{', '.join(_EVENTS)}.")
    if outcome is not None:
        _not_blank("agent outcome", outcome)
        if outcome in RESERVED_OUTCOMES:
            raise ValueRefused(
                f"{outcome!r} is the machinery's own word for an ending "
                "nobody reported, and it is the one ending a later report "
                "may replace. A reporter sending it could have its own "
                "first ending overwritten by its own second one, and no "
                "reader could tell the sweep's guess from something a "
                "harness actually said.")
    moment = _now(now)
    ended = state == EVENT_ENDED
    if parent_agent_id and parent_agent_id == agent_id:
        raise ValueRefused(
            f"a sub-agent cannot have spawned itself, and {agent_id!r} is "
            "named as both the child and its parent. A self-loop is a tree "
            "no reader can draw: the node hangs off nothing and the walk "
            "that finds its parents does not end.")
    values = {
        "id": agent_key(mind_id, agent_id, conversation_ref),
        "mind_id": mind_id,
        "agent_type": agent_type,
        "about": label,
        "conversation_ref": conversation_ref,
        "session_id": session_id,
        # The parent ran in the same mind's process and the same
        # conversation, so it is keyed on the same two things.
        "parent_agent_id": (agent_key(mind_id, parent_agent_id,
                                      conversation_ref)
                            if parent_agent_id else None),
        "task_id": task_id,
        "started_at": moment,
        "active_at": moment if state == EVENT_WORKING else None,
        "finished_at": moment if ended else None,
        "outcome": (outcome or OUTCOME_UNKNOWN) if ended else "",
    }
    sql, args = _sql.insert(AGENTS, values, on_conflict=_AGENT_UPSERT,
                            returning=COLUMNS[AGENTS])
    return (await _execute_returning(unit, sql, *args)).get("row")


def agent_state(row: Row, *, now: float, fresh_within: float) -> str:
    """What this record says the child is doing, read from the row and a clock.

    One definition, because the page, the sweep and anything else reading
    these rows must agree about it - and because the difference between the
    two live states is the whole point of the feature. `RUNNING` is started
    and not yet ended, which is all most harnesses can ever say. `WORKING` is
    a harness positively reporting that the child is still producing work,
    and it expires: `fresh_within` is how long a single activity report may
    stand for, and it is deliberately short. A report that stood for a whole
    turn would have one stamp spin a card for fifteen minutes after the
    child died, which is the indicator claiming activity nobody reported.

    Bounded at both ends, like the page's own freshness check: an `active_at`
    in the future - a reporting host whose clock runs ahead, or a bad event -
    must not read as fresh for as long as the drift lasts.
    """
    if row.get("finished_at") is not None:
        return ENDED
    active = row.get("active_at")
    if active is not None and -fresh_within <= now - active <= fresh_within:
        return WORKING
    return RUNNING


async def retire_stale_agents(*, now: float, timeout: float,
                              alive: Collection[str] | None = None,
                              unit: Unit | None = None) -> list[Row]:
    """Close every open record whose child is gone, and return them.

    A child dies with the process that spawned it. `agent_stale` answers for
    a reader whether a record has outlived its turn; this writes the ending
    down, because a record that only *reads* as dead is live again in every
    other reader.

    `alive` is the conversations still producing output, and it is what makes
    this a reading rather than a guess: a record whose conversation is in it
    is a record whose turn is still running, whatever that mind's turn length
    is, and it is left alone. Pass None to mean "nothing is known about any
    conversation", which retires on the clock alone - correct for a caller
    that has no live feed, and the reason the parameter is optional rather
    than required.

    `timeout` is required and has no default, and applies only to records
    that `alive` cannot speak for. A hive's minds do not share one turn
    length - this hive has 900 seconds on one mind and 14400 on another, and
    a dsh mind can be configured to no bound at all - so a number chosen here
    would retire work still running on the slowest of them.

    Keyed on the last moment the child was known alive, which is its activity
    stamp when it has one and its start when it does not. Keyed on the start
    alone, a child reporting work every minute would be retired mid-report at
    the timeout; and the ending written is that last known moment rather than
    the sweep's own clock, which would claim the child ran until somebody
    happened to look.
    """
    args: list[Any] = [OUTCOME_ABANDONED, now - timeout]
    spared = ""
    if alive:
        args.append(list(alive))
        spared = ('AND ("session_id" IS NULL OR NOT ("session_id" = '
                  f'ANY(${len(args)}::text[]))) ')
    sql = (f"UPDATE {qualified(AGENTS)} "
           f'SET "finished_at" = GREATEST("started_at", '
           f'COALESCE("active_at", "started_at")), '
           f'"outcome" = $1 '
           f'WHERE "finished_at" IS NULL '
           f'AND GREATEST("started_at", COALESCE("active_at", "started_at")) '
           f"< $2 "
           f"{spared}"
           f"RETURNING {select_list(AGENTS)}")
    return await _fetch(unit, sql, *args)


async def agents(*, task_id: str | None = None, session_id: str | None = None,
                 mind_id: str | None = None, since: float | None = None,
                 working: bool = False,
                 unit: Unit | None = None) -> list[Row]:
    """Sub-agent rows, newest first, narrowed by any of the filters.

    `working` keeps only those that have not reported. Note that a child
    still open is not the same as a child still alive - one dies with the
    turn's process and never writes `finished_at` - so a caller drawing
    live work decides staleness from `started_at` and its own clock.

    `session_id`, `mind_id` and `since` are what make a record readable at
    all once harnesses report their own children. Those rows carry no task:
    a harness knows which conversation it is in and nothing about the
    ledger, so a read filtered only by task cannot see one, and the question
    "what ran overnight" is a read across every mind at once. An unfiltered
    read is every child the hive has ever spawned, which is why the page
    narrows by session and the operator narrows by when.
    """
    conditions: dict[str, Any] = {}
    if task_id is not None:
        conditions["task_id"] = task_id
    if session_id is not None:
        conditions["session_id"] = session_id
    if mind_id is not None:
        conditions["mind_id"] = mind_id
    where, args = ("", [])
    if conditions:
        where, args = _sql.where(AGENTS, conditions)
    clauses = [where] if where else []
    if working:
        clauses.append('"finished_at" IS NULL')
    if since is not None:
        args.append(since)
        # Whether the child *ran* in that window, not whether it began in
        # it. A read of the last four hours that matched the start alone
        # missed the child that began at half past eleven and ran until six,
        # which is precisely the overnight work the question is asked about.
        clauses.append(f'("started_at" >= ${len(args)} '
                       f'OR "finished_at" IS NULL '
                       f'OR "finished_at" >= ${len(args)})')
    joined = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return await _fetch(
        unit, f"SELECT {select_list(AGENTS)} FROM {qualified(AGENTS)} "
        f'{joined} ORDER BY "started_at" DESC, "id"', *args)


def agent_stale(row: Row, *, now: float, timeout: float) -> bool:
    """Whether an unreported sub-agent has outlived the turn that spawned it.

    One definition, because the page and anything else reading these rows
    must agree about it. A child lives inside the turn's process, so an open
    row older than the mind's turn timeout is an orphan - the child was
    killed and could not write its own ending - rather than work in flight.
    """
    if row.get("finished_at") is not None:
        return False
    return now - row["started_at"] > timeout


# ----------------------------------------------------------------- sessions

async def attach_session(task_id: str, session_id: str, *,
                         now: float | None = None,
                         unit: Unit | None = None) -> Row:
    """Pair a hive-comms session with a task. Reports rows changed.

    `MissingReference` when the task is not there; `Conflict` when the
    session is already paired, with this task or another. The session is
    not checked against `core.sessions`: the pair is the ledger's record and
    outlives the session row.
    """
    _not_blank("session id", session_id)
    sql, args = _sql.insert(TASK_SESSIONS, {
        "session_id": session_id, "task_id": task_id,
        "attached_at": _now(now),
    })
    return await _execute(unit, sql, *args)


async def detach_session(task_id: str, session_id: str, *,
                         unit: Unit | None = None) -> Row:
    """Unpair a session from a task. Reports rows changed; 0 if unpaired."""
    clause, args = _sql.where(
        TASK_SESSIONS, {"task_id": task_id, "session_id": session_id})
    return await _execute(
        unit, f"DELETE FROM {qualified(TASK_SESSIONS)} WHERE {clause}", *args)


async def task_sessions(task_id: str, *,
                        unit: Unit | None = None) -> list[Row]:
    """The sessions paired with a task, in the order they were attached.

    Ended sessions included: a paired id with no `core.sessions` row is one
    whose conversation has been ended.
    """
    return await _fetch(
        unit, f"SELECT {select_list(TASK_SESSIONS)} "
        f'FROM {qualified(TASK_SESSIONS)} WHERE "task_id" = $1 '
        'ORDER BY "attached_at", "session_id"', task_id)


async def task_for_session(session_id: str, *,
                           unit: Unit | None = None) -> Row | None:
    """The task a session is working, or None when it is paired with none."""
    columns = ", ".join(f't."{c}"' for c in COLUMNS[TASKS])
    return await _fetch_one(
        unit, f"SELECT {columns} FROM {qualified(TASKS)} AS t "
        f'JOIN {qualified(TASK_SESSIONS)} AS p ON p."task_id" = t."id" '
        'WHERE p."session_id" = $1', session_id)
