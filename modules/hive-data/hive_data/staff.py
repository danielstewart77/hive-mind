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
from typing import Any

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

#: What `update_task` may set. `updated_at` is moved by the operation
#: itself, and `id` and `created_at` never move.
_SETTABLE = frozenset({"name", "description", "status", "updated_at"})

#: What the tick may set. Disjoint from `_SETTABLE` on purpose: a status is
#: Daniel's reading of the work, and the machine that watches sessions never
#: writes one. `updated_at` is shared because a stop is a real change to
#: report, and the sitrep finds what moved by that column.
_WATCHABLE = frozenset({"stopped_at", "woken_at", "updated_at"})

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
                      expected_status: Any = UNSET,
                      now: float | None = None,
                      unit: Unit | None = None) -> Row | None:
    """Change a task's name, description or status; `updated_at` moves.

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
        {"name": name, "description": description, "status": status})
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
