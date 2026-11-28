"""The session manager's operations that `core` could not express.

Each one is a statement the gateway's session manager ran against its SQLite
file and that no operation in `core` can say: a join, a condition one column
holds against another, an `OR`, a `CASE`, a "the newest N" read. Without them
the session manager would have to reach past this package, which makes "no
service writes SQL" a slogan.

Kept in a module of their own rather than added to `core`, and every one of
them reads and writes the `core` schema - grouping is by who asked, here,
because these exist for exactly one caller and are named in its words.

The same rules as `core`: every operation takes an optional `unit`, every
read names its columns, and every write reports how many rows it changed.
"""

from __future__ import annotations

from typing import Any, Sequence

from .core import _execute, _fetch, _fetch_one
from .rows import UNSET
from .schema import select_list
from .unit import Unit

Row = dict[str, Any]


def _qualified_list(table: str, alias: str,
                    columns: tuple[str, ...] | None = None) -> str:
    """`select_list`, with every column prefixed by a table alias.

    A join reads two tables that share column names (`session_id`, `id`), so
    an unqualified column list is ambiguous the moment one is added.
    """
    return ", ".join(f"{alias}.{c}" for c in select_list(table, columns).split(", "))


async def find_sessions(
    *, surfaces: Sequence[str] | Any = UNSET,
    surface_id: str | Any = UNSET, surface_id_not: str | Any = UNSET,
    status: str | Any = UNSET, statuses_excluded: Sequence[str] | None = None,
    mind_id: str | Any = UNSET, bound: bool | None = None,
    unit: Unit | None = None,
) -> list[Row]:
    """Sessions by the filters the gateway's own listings are made of.

    Newest first. `core.list_sessions` filters by surface, status and mind,
    and has no word for the three the session pickers and the live view are
    built on: the surface address (`surface_id`), "every surface address but
    this one" (the conversations *another* surface is holding, offered to a
    picker for adoption), and "bound to some surface" (`bound`, the
    conversations somebody could type into right now).

    `UNSET` is "not filtering" so that nothing passed cannot reach the
    driver. An empty `surfaces` matches nothing, for the reason
    `core.list_sessions` gives: it came from a parse, and every session is
    the widest possible answer to the narrowest question.

    Never the private columns. Every listing here answers to the service
    token every surface bot holds.
    """
    conditions: list[str] = []
    args: list[Any] = []
    if surfaces is not UNSET and surfaces is not None:
        surfaces = list(surfaces)
        if not surfaces:
            return []
        args.append(surfaces)
        conditions.append(f's."surface" = ANY(${len(args)})')
    if surface_id is not UNSET:
        args.append(surface_id)
        conditions.append(f's."surface_id" = ${len(args)}')
    if surface_id_not is not UNSET:
        args.append(surface_id_not)
        # `IS DISTINCT FROM`, not `<>`: a row with no address is not the
        # address being excluded, and `NULL <> x` is NULL, which drops it.
        conditions.append(f's."surface_id" IS DISTINCT FROM ${len(args)}')
    if status is not UNSET:
        if status is None:
            conditions.append('s."status" IS NULL')
        else:
            args.append(status)
            conditions.append(f's."status" = ${len(args)}')
    if statuses_excluded:
        args.append(list(statuses_excluded))
        conditions.append(f'NOT (s."status" = ANY(${len(args)}))')
    if mind_id is not UNSET and mind_id is not None:
        args.append(mind_id)
        conditions.append(f's."mind_id" = ${len(args)}')
    if bound is not None:
        conditions.append(
            ('' if bound else 'NOT ') +
            'EXISTS (SELECT 1 FROM core."active_sessions" a '
            'WHERE a."session_id" = s."id")')
    sql = f'SELECT {_qualified_list("sessions", "s")} FROM core."sessions" s'
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += ' ORDER BY s."last_active" DESC, s."id" DESC'
    return await _fetch(unit, sql, *args)


async def active_session_id(
    client_type: str, client_ref: str, *,
    statuses_excluded: Sequence[str] = (),
    unit: Unit | None = None,
) -> str | None:
    """The session a surface is bound to, unless that session is retired.

    One statement over the binding and the row together. Read as two, a
    binding pointing at a closed session answers with the session, and the
    surface resurrects a conversation somebody ended - which is the zombie
    this read exists to refuse.
    """
    args: list[Any] = [client_type, client_ref]
    sql = ('SELECT a."session_id" FROM core."active_sessions" a '
           'JOIN core."sessions" s ON s."id" = a."session_id" '
           'WHERE a."client_type" = $1 AND a."client_ref" = $2')
    if statuses_excluded:
        args.append(list(statuses_excluded))
        sql += f' AND NOT (s."status" = ANY(${len(args)}))'
    row = await _fetch_one(unit, sql, *args)
    return None if row is None else row["session_id"]


async def session_labels(*, unit: Unit | None = None) -> list[Row]:
    """Every session's operator-given name and colour, where it has either.

    A picker draws a whole rail of conversations from one read, so this is
    the three columns it needs across every row that carries a label, closed
    rows included: a surface showing history draws a named conversation as
    unnamed otherwise.
    """
    return await _fetch(
        unit,
        'SELECT "id", "name", "color" FROM core."sessions" '
        'WHERE "name" IS NOT NULL OR "color" IS NOT NULL ORDER BY "id"')


async def record_unmeasured_context(
    session_id: str, *, observed_at: float, tokens: int,
    threshold: int | None = None, window: int | None = None,
    unit: Unit | None = None,
) -> Row:
    """A context report that measured nothing, written without erasing a size.

    The reporter could not read its transcript. A size already on the row -
    the estimate a fresh conversation opened with among them - stands with
    its own kind, age and model, because a zero written over it fits every
    model and a fresh timestamp makes a stale figure look current. Only a row
    holding no size at all takes the report.

    One statement with the test inside it rather than a read and a write: a
    real report landing between the two would be overwritten by "nothing".
    A threshold or window is written only when positive, since a room of
    nothing is a model nobody measured.

    The row count says whether the session still exists.
    """
    assignments = [
        '"context_kind" = CASE WHEN "context_tokens" IS NULL '
        "THEN 'none' ELSE \"context_kind\" END",
        '"context_observed_at" = CASE WHEN "context_tokens" IS NULL '
        'THEN $2 ELSE "context_observed_at" END',
        '"context_tokens" = COALESCE("context_tokens", $3)',
    ]
    args: list[Any] = [session_id, observed_at, tokens]
    for column, value in (("context_threshold", threshold),
                          ("context_window", window)):
        if value is not None and int(value) > 0:
            args.append(int(value))
            assignments.append(f'"{column}" = ${len(args)}')
    return await _execute(
        unit,
        f'UPDATE core."sessions" SET {", ".join(assignments)} WHERE "id" = $1',
        *args)


async def clear_carry_forward(
    session_id: str, *, typed_into: str | None = None,
    unit: Unit | None = None,
) -> Row:
    """Drop a stored carry-forward once a turn shows it has landed.

    `typed_into` names the conversation the turn was typed into, and scopes
    the clear to a seed composed *for* it. A rotation's composition runs for
    minutes while the user keeps typing, so a straggling turn from the old
    conversation can arrive after the new seed is written; unscoped, it
    clears a seed it never saw, and the pane the seed was meant for opens
    bare.

    Either id names the current conversation: the gateway's own, or - for a
    codex hook, which knows only the thread codex minted - the row's
    `harness_sid`, when the seed was composed for the gateway's id beside
    it. That second arm compares one column with another, which an equality
    guard cannot say.
    """
    clear = ('UPDATE core."sessions" SET "carry_forward" = NULL, '
             '"carry_forward_sid" = NULL, "carry_forward_at" = NULL '
             'WHERE "id" = $1')
    if typed_into is None:
        return await _execute(unit, clear, session_id)
    return await _execute(
        unit,
        clear + ' AND ("carry_forward_sid" = $2 OR ("harness_sid" = $2 '
        'AND "carry_forward_sid" = "conversation_id"))',
        session_id, typed_into)


async def least_overhead_on_surface(
    *, mind_id: str, harness: str, surface: str, unit: Unit | None = None,
) -> int | None:
    """The lowest overhead measured for this mind and harness on one surface.

    `core.least_overhead` reads across every surface, and a pane's
    interactive harness and a chat process ship different prompts, so a
    pane's figure stood in for a process would size every chat opening
    wrong. Across every model, which is what the gateway falls back to when
    it has no reading for the exact model.
    """
    row = await _fetch_one(
        unit,
        'SELECT min("tokens") AS "tokens" FROM core."context_overhead" '
        'WHERE "mind_id" = $1 AND "harness" = $2 AND "surface" = $3',
        mind_id, harness, surface)
    return None if row is None or row["tokens"] is None else int(row["tokens"])


async def latest_session_memory_for(
    conversation_ids: Sequence[str], *, unit: Unit | None = None,
) -> Row | None:
    """The newest carried-forward memory filed under any of these ids.

    A rotation hook files its summary under whichever id it knows - the
    gateway's conversation id, or codex's own thread id - so the gateway
    asks with both and the newest wins. Identity breaks a timestamp tie.
    An empty list asks about nothing and is answered with nothing.
    """
    ids = [i for i in conversation_ids if i]
    if not ids:
        return None
    return await _fetch_one(
        unit,
        f'SELECT {select_list("session_memory")} FROM core."session_memory" '
        'WHERE "conversation_id" = ANY($1) '
        'ORDER BY "created_at" DESC, "id" DESC LIMIT 1',
        ids)


async def last_turns(session_id: str, count: int, *,
                     unit: Unit | None = None) -> list[Row]:
    """A session's last `count` turns, oldest first.

    Read newest-first in the database and turned round, rather than the
    whole ledger shipped here to keep its tail: a pane that ran for a week
    holds thousands of turns, and the caller wants two. Identity breaks the
    timestamp tie both ways, or a turn and its reply written in one second
    come back in plan order.
    """
    rows = await _fetch(
        unit,
        f'SELECT {select_list("session_turns")} FROM core."session_turns" '
        'WHERE "session_id" = $1 '
        'ORDER BY "created_at" DESC, "id" DESC LIMIT $2',
        session_id, count)
    rows.reverse()
    return rows


async def mint_missing_conversation_ids(*, unit: Unit | None = None) -> Row:
    """Give every session with no conversation id a fresh one.

    A session owns one conversation from birth, and a row from before that
    rule may still be blank: one that never finished a turn, so there is
    nothing on disk to lose. Minted in the database because the value is a
    fresh random id per row, which one bound parameter cannot be.
    """
    return await _execute(
        unit,
        'UPDATE core."sessions" SET "conversation_id" = gen_random_uuid()::text '
        'WHERE "conversation_id" IS NULL OR "conversation_id" = \'\'')

