"""Named operations over the `core` schema.

Grouped by schema rather than by service, because the whole reason the hive
has one database is that comms can read what the console wrote. Per-service
query modules would re-duplicate the session queries the first time a second
service needed them, which is the duplication the shared surfaces package
was built to end.

Every operation takes an optional `unit`. Given one it runs inside that
caller's transaction; given none it runs on its own connection and commits by
itself. Every read names its columns. Every write reports how many rows it
changed.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import _sql, pool
from .errors import UnsettableColumn, ValueRefused
from .rows import UNSET, as_row, as_rows, row_count, written
from .schema import COLUMNS, select_list
from .unit import Unit

Row = dict[str, Any]


async def _rows(connection: Any, sql: str, *args: Any) -> list[Row]:
    return as_rows(await connection.fetch(sql, *args))


async def _fetch(unit: Unit | None, sql: str, *args: Any) -> list[Row]:
    """Every matching row, as plain dicts. Empty list when nothing matches."""
    if unit is not None:
        return await unit.fetch(sql, *args)
    return await pool.run(lambda c: _rows(c, sql, *args))


async def _fetch_one(unit: Unit | None, sql: str, *args: Any) -> Row | None:
    """The first matching row, or None.

    None rather than an empty dict: an empty dict is a row with no columns,
    and a caller testing it for truth cannot tell the two apart.
    """
    async def _one(connection: Any) -> Row | None:
        return as_row(await connection.fetchrow(sql, *args))

    if unit is not None:
        return await unit.fetch_one(sql, *args)
    return await pool.run(_one)


async def _execute(unit: Unit | None, sql: str, *args: Any) -> Row:
    """Run a write and report how many rows it changed."""
    async def _run(connection: Any) -> Row:
        return written(row_count(await connection.execute(sql, *args)))

    if unit is not None:
        return await unit.execute(sql, *args)
    return await pool.run(_run)


async def _execute_returning(unit: Unit | None, sql: str, *args: Any) -> Row:
    """Run a write that returns a row, and report whether it landed."""
    async def _run(connection: Any) -> Row:
        row = as_row(await connection.fetchrow(sql, *args))
        return written(0 if row is None else 1, row)

    if unit is not None:
        return await unit.execute_returning(sql, *args)
    return await pool.run(_run)


# ---------------------------------------------------------------------------
# minds
# ---------------------------------------------------------------------------

async def get_mind(mind_id: str, *, unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("minds")} FROM core."minds" '
        'WHERE "mind_id" = $1',
        mind_id)


async def get_mind_by_name(name: str, *,
                           unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("minds")} FROM core."minds" WHERE "name" = $1',
        name)


async def list_minds(*, unit: Unit | None = None) -> list[Row]:
    return await _fetch(
        unit,
        f'SELECT {select_list("minds")} FROM core."minds" ORDER BY "name"')


async def upsert_mind(
    *, mind_id: str, name: str, gateway_url: str, model: str, harness: str,
    registered_at: float, last_seen: float,
    session_token: str | Any = UNSET, voice: str | Any = UNSET,
    voice_engine: str | Any = UNSET, owner: str | Any = UNSET,
    unit: Unit | None = None,
) -> Row:
    """Register a mind, or update the registration it already has.

    An upsert rather than an insert: `mind_server` re-registers from its own
    `runtime.yaml` on every start, which is what makes the file authoritative
    and the row a cache of it. An insert would refuse every boot after the
    first.
    """
    values = _sql.passed({
        "mind_id": mind_id, "name": name, "gateway_url": gateway_url,
        "model": model, "harness": harness, "registered_at": registered_at,
        "last_seen": last_seen, "session_token": session_token,
        "voice": voice, "voice_engine": voice_engine, "owner": owner,
    })
    # On `mind_id`, which is what a registration is an upsert on. The
    # primary key happens to be `name`, and conflicting on that is how a
    # second install scaffolded with the same MIND_NAME silently takes over
    # an existing mind's row and rewrites its UUID — orphaning every graph
    # node, memory write and surface claim keyed to the old one. Conflicting
    # on the identity also makes renaming a mind work, where the other way
    # round it failed registration on every boot.
    updatable = [c for c in values if c != "mind_id"]
    assignments = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in updatable)
    sql, args = _sql.insert(
        "minds", values,
        on_conflict=f'ON CONFLICT ("mind_id") DO UPDATE SET {assignments}',
        returning=COLUMNS["minds"])
    return await _execute_returning(unit, sql, *args)


async def touch_mind(mind_id: str, last_seen: float, *,
                     unit: Unit | None = None) -> Row:
    sql, args = _sql.update(
        "minds", {"last_seen": last_seen}, {"mind_id": mind_id},
        settable=frozenset({"last_seen"}))
    return await _execute(unit, sql, *args)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

#: What a partial session update may set. Everything that is not identity and
#: not the row's own creation facts. Declared because the first consumer's
#: dominant write assembles its SET clause at runtime, and a caller's keys
#: must never reach the statement.
SESSION_SETTABLE = frozenset({
    "conversation_id", "surface", "surface_id", "opening_line", "model",
    "last_active", "status", "mind_id", "rotation_armed", "harness_sid",
    "carry_forward", "carry_forward_sid", "carry_forward_at",
    "carry_forward_prompt_cap", "context_tokens", "context_threshold",
    "context_window", "context_observed_at", "name", "color", "effort",
    "harness", "terminal_held", "context_kind", "context_has_images",
    "context_model", "resend_content", "resend_images", "resend_goal_rounds",
    "resend_dispatched_at", "conversation_started_at",
    "context_opening_tokens", "context_opening_surface",
})


async def create_session(
    *, id: str, surface: str, surface_id: str, mind_id: str,
    created_at: float, last_active: float, status: str = "running",
    conversation_id: str | Any = UNSET, model: str | Any = UNSET,
    opening_line: str | Any = UNSET, harness: str | Any = UNSET,
    effort: str | Any = UNSET, name: str | Any = UNSET,
    color: str | Any = UNSET, conversation_started_at: float | Any = UNSET,
    unit: Unit | None = None,
) -> Row:
    values = _sql.passed({
        "id": id, "surface": surface, "surface_id": surface_id,
        "mind_id": mind_id, "created_at": created_at,
        "last_active": last_active, "status": status,
        "conversation_id": conversation_id, "model": model,
        "opening_line": opening_line, "harness": harness, "effort": effort,
        "name": name, "color": color,
        "conversation_started_at": conversation_started_at,
    })
    sql, args = _sql.insert("sessions", values, returning=("id",))
    return await _execute_returning(unit, sql, *args)


async def get_session(session_id: str, *, private: bool = False,
                      unit: Unit | None = None) -> Row | None:
    """One session row.

    `private` has to be asked for. The carry-forward and resend columns hold
    a composed system prompt and a queued user message, and the listings that
    read sessions answer to the service token every surface bot holds.
    """
    columns = COLUMNS["sessions"] if private else None
    return await _fetch_one(
        unit,
        f'SELECT {select_list("sessions", columns)} FROM core."sessions" '
        'WHERE "id" = $1',
        session_id)


async def get_session_by_conversation(
    conversation_id: str, *, unit: Unit | None = None,
) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("sessions")} FROM core."sessions" '
        'WHERE "conversation_id" = $1',
        conversation_id)


async def list_sessions(
    *, surfaces: Sequence[str] | Any = UNSET, status: str | Any = UNSET,
    mind_id: str | Any = UNSET,
    statuses_excluded: Sequence[str] | None = None,
    limit: int | None = None, private: bool = False,
    unit: Unit | None = None,
) -> list[Row]:
    """Sessions, filtered by surface, status and mind.

    The filters are separate parameters rather than a condition string: a
    caller that could pass SQL would pass SQL. They read `UNSET` for "not
    filtering" so that `None` can mean `IS NULL`, which is the same rule the
    writes follow — and so a stray `UNSET` cannot reach the driver and come
    back as a domain error a caller might branch on.
    """
    conditions: list[str] = []
    args: list[Any] = []
    if surfaces is not UNSET and surfaces is not None:
        surfaces = list(surfaces)
        if not surfaces:
            # An empty list is a filter that matches nothing, not an absent
            # filter. The caller built it from a parse, and silently
            # returning every session instead would be the widest possible
            # answer to the narrowest possible question.
            return []
        args.append(surfaces)
        conditions.append(f'"surface" = ANY(${len(args)})')
    if status is not UNSET:
        if status is None:
            conditions.append('"status" IS NULL')
        else:
            args.append(status)
            conditions.append(f'"status" = ${len(args)}')
    if mind_id is not UNSET and mind_id is not None:
        args.append(mind_id)
        conditions.append(f'"mind_id" = ${len(args)}')
    if statuses_excluded:
        args.append(list(statuses_excluded))
        conditions.append(f'NOT ("status" = ANY(${len(args)}))')
    columns = COLUMNS["sessions"] if private else None
    sql = f'SELECT {select_list("sessions", columns)} FROM core."sessions"'
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += ' ORDER BY "last_active" DESC, "id" DESC'
    if limit is not None:
        args.append(limit)
        sql += f" LIMIT ${len(args)}"
    return await _fetch(unit, sql, *args)


async def update_session(session_id: str, *, unit: Unit | None = None,
                         **fields: Any) -> Row:
    """Set some subset of a session's columns, and report whether it landed.

    The row count is the answer to "is there still a session with that id",
    which the first consumer asks by exactly this means: a context report for
    a session that has been swept must not answer `ok`.
    """
    sql, args = _sql.update(
        "sessions", _sql.passed(fields), {"id": session_id},
        settable=SESSION_SETTABLE)
    return await _execute(unit, sql, *args)


async def update_session_if(
    session_id: str, *, expected: dict[str, Any],
    unit: Unit | None = None, **fields: Any,
) -> Row:
    """A conditional update: set these, only while those still hold.

    The row count is the arbitration. Two callers racing to claim the same
    conversation both issue this; exactly one of them is told it changed a
    row, and the other is told it changed none. A write that could only
    answer with rows would answer `[]` to both.
    """
    if not expected:
        raise UnsettableColumn(
            "a conditional update with nothing expected is an unconditional "
            "update that reports it won a race it never entered. The row "
            "count is the arbitration, so `rows: 1` from an empty guard "
            "would tell every contender it was the winner. Name what has to "
            "still hold, or call update_session.")
    values = _sql.passed(fields)
    clause, extra_args = _sql.where(
        "sessions", expected, start=len(values) + 2)
    sql, args = _sql.update(
        "sessions", values, {"id": session_id},
        settable=SESSION_SETTABLE, extra_where=clause)
    return await _execute(unit, sql, *args, *extra_args)


async def delete_session(session_id: str, *, unit: Unit | None = None) -> Row:
    """Delete a session. Its turns and surface binding go with it.

    Both are declared ON DELETE CASCADE in the applied schema, because a
    turn ledger and a surface binding are parts of their session rather than
    things that outlive it.
    """
    return await _execute(
        unit, 'DELETE FROM core."sessions" WHERE "id" = $1', session_id)


# ---------------------------------------------------------------------------
# the turn ledger
# ---------------------------------------------------------------------------

async def record_turn(
    *, session_id: str, role: str, content: str, created_at: float,
    unit: Unit | None = None,
) -> Row:
    """Append a turn, and hand back the identity the database assigned.

    The identity comes back because the caller deletes by it. It is never
    passed in: Postgres accepts a value on a generated column without
    advancing the sequence, so a supplied id lands and the next insert
    collides.
    """
    sql, args = _sql.insert(
        "session_turns",
        {"session_id": session_id, "role": role, "content": content,
         "created_at": created_at},
        returning=("id", "session_id", "role", "content", "created_at"))
    return await _execute_returning(unit, sql, *args)


async def list_turns(session_id: str, *, since: float | None = None,
                     limit: int | None = None,
                     unit: Unit | None = None) -> list[Row]:
    """A session's turns, oldest first.

    Ordered by identity as well as by time, and the identity is what breaks
    the tie. `created_at` is whole epoch seconds and `now()` is constant for
    a transaction, so two turns written in one unit of work carry the same
    timestamp — and an order without the tiebreak returns them in whichever
    order the plan happened to produce. The visible failure is the mind
    answering before it was asked.
    """
    args: list[Any] = [session_id]
    sql = (f'SELECT {select_list("session_turns")} FROM core."session_turns" '
           'WHERE "session_id" = $1')
    if since is not None:
        args.append(since)
        sql += f' AND "created_at" >= ${len(args)}'
    sql += ' ORDER BY "created_at" ASC, "id" ASC'
    if limit is not None:
        args.append(limit)
        sql += f" LIMIT ${len(args)}"
    return await _fetch(unit, sql, *args)


async def latest_turn(session_id: str, *, role: str | None = None,
                      unit: Unit | None = None) -> Row | None:
    """The most recent turn, with identity breaking a timestamp tie."""
    args: list[Any] = [session_id]
    sql = (f'SELECT {select_list("session_turns")} FROM core."session_turns" '
           'WHERE "session_id" = $1')
    if role is not None:
        args.append(role)
        sql += f' AND "role" = ${len(args)}'
    sql += ' ORDER BY "created_at" DESC, "id" DESC LIMIT 1'
    return await _fetch_one(unit, sql, *args)


# ---------------------------------------------------------------------------
# which surface holds which session
# ---------------------------------------------------------------------------

async def bind_surface(
    *, client_type: str, client_ref: str, session_id: str,
    unit: Unit | None = None,
) -> Row:
    """Point a surface's conversation at a session, replacing any binding.

    An upsert, because the primary key is the surface and its address: a
    plain insert fails the first time somebody switches which session their
    Telegram chat is on. The first consumer writes this as a delete followed
    by an insert, which run twice at once leaves two rows for one session
    and no constraint catches it.
    """
    sql, args = _sql.insert(
        "active_sessions",
        {"client_type": client_type, "client_ref": client_ref,
         "session_id": session_id},
        on_conflict=('ON CONFLICT ("client_type", "client_ref") '
                     'DO UPDATE SET "session_id" = EXCLUDED."session_id"'),
        returning=COLUMNS["active_sessions"])
    return await _execute_returning(unit, sql, *args)


async def bound_session(client_type: str, client_ref: str, *,
                        unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("active_sessions")} '
        'FROM core."active_sessions" '
        'WHERE "client_type" = $1 AND "client_ref" = $2',
        client_type, client_ref)


async def unbind_surface(client_type: str, client_ref: str, *,
                         unit: Unit | None = None) -> Row:
    return await _execute(
        unit,
        'DELETE FROM core."active_sessions" '
        'WHERE "client_type" = $1 AND "client_ref" = $2',
        client_type, client_ref)


# ---------------------------------------------------------------------------
# who a mind's bot answers
# ---------------------------------------------------------------------------

async def get_claim(mind_id: str, surface: str, *,
                    unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("surface_claims")} FROM core."surface_claims" '
        'WHERE "mind_id" = $1 AND "surface" = $2',
        mind_id, surface)


async def claim_surface_if_unclaimed(
    *, mind_id: str, surface: str, claimed_ref: str, claimed_at: float,
    unit: Unit | None = None,
) -> Row:
    """Take an unclaimed bot, and report whether this caller is the one who did.

    The row count is the whole arbitration. Two people sending the claim
    phrase to one bot at the same moment both run this; the one told it
    changed a row owns the bot, and the other is told it changed none. That
    is why a write reports its count: answering both with an empty list
    would confirm two owners.
    """
    return await _execute(
        unit,
        'UPDATE core."surface_claims" '
        'SET "claimed_ref" = $3, "claimed_at" = $4, "phrase_hash" = NULL, '
        '"phrase_issued_at" = NULL, "phrase_expires_at" = NULL, '
        '"released_at" = NULL '
        'WHERE "mind_id" = $1 AND "surface" = $2 AND "claimed_ref" IS NULL',
        mind_id, surface, claimed_ref, claimed_at)


async def count_phrase_attempt(
    *, mind_id: str, surface: str, unit: Unit | None = None,
) -> Row:
    """Add one to the guess counter and hand back what it now stands at.

    Incremented in the statement that reads it. Read-modify-write counts
    rounds rather than guesses: parallel attempts all read the same number
    and all write the same number, so the lockout that stops somebody taking
    an unclaimed bot by brute force never arrives.
    """
    return await _execute_returning(
        unit,
        'UPDATE core."surface_claims" '
        'SET "phrase_attempts" = "phrase_attempts" + 1 '
        'WHERE "mind_id" = $1 AND "surface" = $2 '
        'RETURNING "mind_id", "surface", "phrase_attempts"',
        mind_id, surface)


async def upsert_claim(
    *, mind_id: str, surface: str, claimed_ref: str | Any = UNSET,
    claimed_at: float | Any = UNSET, phrase_hash: str | Any = UNSET,
    phrase_issued_at: float | Any = UNSET,
    phrase_expires_at: float | Any = UNSET, bot_ref: str | Any = UNSET,
    phrase_attempts: int | Any = UNSET, released_at: float | Any = UNSET,
    unit: Unit | None = None,
) -> Row:
    values = _sql.passed({
        "mind_id": mind_id, "surface": surface, "claimed_ref": claimed_ref,
        "claimed_at": claimed_at, "phrase_hash": phrase_hash,
        "phrase_issued_at": phrase_issued_at,
        "phrase_expires_at": phrase_expires_at, "bot_ref": bot_ref,
        "phrase_attempts": phrase_attempts, "released_at": released_at,
    })
    # No `setdefault` here. Defaulting an unpassed column puts it in the
    # DO UPDATE SET, and `phrase_attempts` is the brute-force lockout on an
    # unclaimed bot: any unrelated claim write would put the counter back to
    # zero and the lockout would never arrive. The column is NOT NULL
    # DEFAULT 0 in the schema, so a fresh row needs nothing from here.
    updatable = [c for c in values if c not in ("mind_id", "surface")]
    assignments = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in updatable)
    sql, args = _sql.insert(
        "surface_claims", values,
        on_conflict=(f'ON CONFLICT ("mind_id", "surface") DO UPDATE SET '
                     f'{assignments}' if updatable else
                     'ON CONFLICT ("mind_id", "surface") DO NOTHING'),
        returning=COLUMNS["surface_claims"])
    return await _execute_returning(unit, sql, *args)


# ---------------------------------------------------------------------------
# broker conversations and their messages
# ---------------------------------------------------------------------------

async def create_conversation(*, id: str, created_at: float,
                              unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "conversations", {"id": id, "created_at": created_at},
        on_conflict='ON CONFLICT ("id") DO NOTHING', returning=("id",))
    return await _execute_returning(unit, sql, *args)


async def add_message(
    *, id: str, conversation_id: str, from_mind: str, to_mind: str,
    message_number: int, content: str, status: str, timestamp: float,
    rolling_summary: str | Any = UNSET, metadata: str | Any = UNSET,
    recipient_session_id: str | Any = UNSET,
    response_error: str | Any = UNSET, unit: Unit | None = None,
) -> Row:
    """Add a message, treating a duplicate as already done.

    Deliberately idempotent, and the row count is how the caller knows which
    it was. The SMS gateway re-POSTs the same webhook on any non-2xx, so a
    duplicate that raised would become a 500 and be retried forever — which
    is why a uniqueness conflict here is benign and reported as nothing
    inserted rather than as a rejected write.
    """
    values = _sql.passed({
        "id": id, "conversation_id": conversation_id, "from_mind": from_mind,
        "to_mind": to_mind, "message_number": message_number,
        "content": content, "status": status, "timestamp": timestamp,
        "rolling_summary": rolling_summary, "metadata": metadata,
        "recipient_session_id": recipient_session_id,
        "response_error": response_error,
    })
    sql, args = _sql.insert(
        "messages", values, on_conflict='ON CONFLICT ("id") DO NOTHING',
        returning=("id",))
    return await _execute_returning(unit, sql, *args)


async def list_messages(conversation_id: str, *, limit: int | None = None,
                        unit: Unit | None = None) -> list[Row]:
    args: list[Any] = [conversation_id]
    sql = (f'SELECT {select_list("messages")} FROM core."messages" '
           'WHERE "conversation_id" = $1 '
           'ORDER BY "message_number" ASC, "id" ASC')
    if limit is not None:
        args.append(limit)
        sql += f" LIMIT ${len(args)}"
    return await _fetch(unit, sql, *args)


async def update_message(message_id: str, *, unit: Unit | None = None,
                         **fields: Any) -> Row:
    sql, args = _sql.update(
        "messages", _sql.passed(fields), {"id": message_id},
        settable=frozenset({
            "content", "rolling_summary", "metadata", "status",
            "recipient_session_id", "response_error"}))
    return await _execute(unit, sql, *args)


# ---------------------------------------------------------------------------
# carried-forward session memory
# ---------------------------------------------------------------------------

async def add_session_memory(
    *, mind_id: str, mind_name: str, client_ref: str, conversation_id: str,
    body: str, created_at: float, unit: Unit | None = None,
) -> Row:
    sql, args = _sql.insert(
        "session_memory",
        {"mind_id": mind_id, "mind_name": mind_name,
         "client_ref": client_ref, "conversation_id": conversation_id,
         "body": body, "created_at": created_at},
        returning=COLUMNS["session_memory"])
    return await _execute_returning(unit, sql, *args)


async def latest_session_memory(
    *, mind_id: str, client_ref: str, unit: Unit | None = None,
) -> Row | None:
    """The most recent carried-forward memory for this conversation.

    Read by mind and client reference, never by session: the
    `conversation_id` column is a provenance stamp rather than a reference,
    which is exactly what its old name hid.
    """
    return await _fetch_one(
        unit,
        f'SELECT {select_list("session_memory")} FROM core."session_memory" '
        'WHERE "mind_id" = $1 AND "client_ref" = $2 '
        'ORDER BY "created_at" DESC, "id" DESC LIMIT 1',
        mind_id, client_ref)


# ---------------------------------------------------------------------------
# credentials, settings, scopes, people
# ---------------------------------------------------------------------------

async def get_credential(holder: str, *,
                         unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("credentials")} FROM core."credentials" '
        'WHERE "holder" = $1',
        holder)


async def upsert_credential(
    *, holder: str, username: str, role: str, token_sha: str,
    created_at: float, revoked_at: float | Any = UNSET,
    unit: Unit | None = None,
) -> Row:
    values = _sql.passed({
        "holder": holder, "username": username, "role": role,
        "token_sha": token_sha, "created_at": created_at,
        "revoked_at": revoked_at,
    })
    updatable = [c for c in values if c != "holder"]
    assignments = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in updatable)
    sql, args = _sql.insert(
        "credentials", values,
        on_conflict=f'ON CONFLICT ("holder") DO UPDATE SET {assignments}',
        returning=COLUMNS["credentials"])
    return await _execute_returning(unit, sql, *args)


async def revoke_credential(holder: str, revoked_at: float, *,
                            unit: Unit | None = None) -> Row:
    sql, args = _sql.update(
        "credentials", {"revoked_at": revoked_at}, {"holder": holder},
        settable=frozenset({"revoked_at"}))
    return await _execute(unit, sql, *args)


async def get_setting(key: str, *, unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("settings")} FROM core."settings" '
        'WHERE "key" = $1',
        key)


async def put_setting(key: str, value: str, updated_at: float, *,
                      unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "settings", {"key": key, "value": value, "updated_at": updated_at},
        on_conflict=('ON CONFLICT ("key") DO UPDATE SET '
                     '"value" = EXCLUDED."value", '
                     '"updated_at" = EXCLUDED."updated_at"'),
        returning=COLUMNS["settings"])
    return await _execute_returning(unit, sql, *args)


async def grant_secret_scope(*, mind_name: str, secret_key: str,
                             granted_at: float,
                             unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "secret_scopes",
        {"mind_name": mind_name, "secret_key": secret_key,
         "granted_at": granted_at},
        on_conflict='ON CONFLICT ("mind_name", "secret_key") DO NOTHING',
        returning=COLUMNS["secret_scopes"])
    return await _execute_returning(unit, sql, *args)


async def list_secret_scopes(mind_name: str, *,
                             unit: Unit | None = None) -> list[Row]:
    return await _fetch(
        unit,
        f'SELECT {select_list("secret_scopes")} FROM core."secret_scopes" '
        'WHERE "mind_name" = $1 ORDER BY "secret_key"',
        mind_name)


async def get_user(username: str, *, private: bool = False,
                   unit: Unit | None = None) -> Row | None:
    """One person's record by name.

    `private` has to be asked for, and `password_hash` is the one column it
    adds. Sign-in needs it and nothing else does: every other read of a
    person is rendered on a page or handed to a route table, and a hash that
    travels by default is a hash in a log the day somebody dumps a row.
    """
    columns = COLUMNS["users"] if private else None
    return await _fetch_one(
        unit,
        f'SELECT {select_list("users", columns)} FROM core."users" '
        'WHERE "username" = $1',
        username)


async def get_user_by_id(user_id: int, *, private: bool = False,
                         unit: Unit | None = None) -> Row | None:
    """One person's record by the id a session cookie carries."""
    columns = COLUMNS["users"] if private else None
    return await _fetch_one(
        unit,
        f'SELECT {select_list("users", columns)} FROM core."users" '
        'WHERE "id" = $1',
        user_id)


async def list_users(*, unit: Unit | None = None) -> list[Row]:
    """Everybody with an account, by name."""
    return await _fetch(
        unit,
        f'SELECT {select_list("users")} FROM core."users" ORDER BY "username"')


async def count_users(*, unit: Unit | None = None) -> int:
    """How many accounts there are, for the install that has to seed one."""
    row = await _fetch_one(unit, 'SELECT count(*) AS "n" FROM core."users"')
    assert row is not None  # an aggregate always answers
    return int(row["n"])


async def find_user(*, email: str | None = None,
                    domain_account: str | None = None,
                    unit: Unit | None = None) -> Row | None:
    """The account an outside identity names, case-insensitively.

    One of the two, named. Both at once is refused rather than resolved by
    precedence: a caller holding an email and a domain account that name
    different people has a question this operation cannot answer, and
    whichever one happened to win would sign somebody in as somebody else.
    """
    if (email is None) == (domain_account is None):
        raise ValueRefused(
            "find_user takes exactly one of email or domain_account. Neither "
            "names nobody, and both is two identities that may name two "
            "people - which is a sign-in as whichever one the code happened "
            "to check first.")
    column = "email" if email is not None else "domain_account"
    value = (email if email is not None else domain_account) or ""
    value = value.strip()
    if not value:
        return None
    return await _fetch_one(
        unit,
        f'SELECT {select_list("users")} FROM core."users" '
        f'WHERE lower("{column}") = lower($1)',
        value)


async def groups_of(user_id: int, *, unit: Unit | None = None) -> list[str]:
    """The groups a person is in, by name.

    A list of names rather than rows: membership is read on every request to
    decide what a page may show, and the only thing read off the row is the
    group's name.
    """
    rows = await _fetch(
        unit,
        'SELECT "group_name" FROM core."group_members" '
        'WHERE "user_id" = $1 ORDER BY "group_name"',
        user_id)
    return [row["group_name"] for row in rows]


async def group_usernames(group_name: str, *,
                          unit: Unit | None = None) -> list[str]:
    """Who is in a group, by name, as the groups page lists them."""
    rows = await _fetch(
        unit,
        'SELECT u."username" FROM core."group_members" AS m '
        'JOIN core."users" AS u ON u."id" = m."user_id" '
        'WHERE m."group_name" = $1 ORDER BY u."username"',
        group_name)
    return [row["username"] for row in rows]


async def get_group(name: str, *, unit: Unit | None = None) -> Row | None:
    """One group, or None when there is no such group."""
    return await _fetch_one(
        unit,
        f'SELECT {select_list("groups")} FROM core."groups" '
        'WHERE "name" = $1',
        name)


async def enabled_members_besides(*, group_name: str, user_id: int,
                                  unit: Unit | None = None) -> int:
    """How many enabled people are in a group, not counting this one.

    The last-admin guard, as one statement. Counted in the database inside
    the caller's unit rather than by listing the group and filtering in
    Python: two admins disabling each other at the same moment both read a
    list that still holds the other, and both pass a check made against it.
    """
    row = await _fetch_one(
        unit,
        'SELECT count(*) AS "n" FROM core."users" AS u '
        'JOIN core."group_members" AS m ON m."user_id" = u."id" '
        'WHERE m."group_name" = $1 AND u."disabled_at" IS NULL '
        'AND u."id" <> $2',
        group_name, user_id)
    assert row is not None
    return int(row["n"])


async def bump_session_version(user_id: int, *,
                               unit: Unit | None = None) -> Row:
    """End every session this person holds, by moving the version they carry.

    An increment rather than a value the caller computed, because a caller
    that read the version, added one and wrote it back loses the other
    writer's bump - and the whole point of the column is that a disable or a
    password reset ends a session that is open right now. The new version is
    returned so a caller reissuing a cookie does not have to read it again.
    """
    return await _execute_returning(
        unit,
        'UPDATE core."users" SET "session_version" = "session_version" + 1 '
        'WHERE "id" = $1 RETURNING "id", "username", "session_version"',
        user_id)


async def create_user(
    *, username: str, password_hash: str, created_at: int,
    must_change: int = 0, session_version: int = 0,
    email: str | Any = UNSET, domain_account: str | Any = UNSET,
    unit: Unit | None = None,
) -> Row:
    values = _sql.passed({
        "username": username, "password_hash": password_hash,
        "created_at": created_at, "must_change": must_change,
        "session_version": session_version, "email": email,
        "domain_account": domain_account,
    })
    sql, args = _sql.insert("users", values, returning=COLUMNS["users"])
    return await _execute_returning(unit, sql, *args)


async def add_group_member(*, group_name: str, user_id: int,
                           unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "group_members", {"group_name": group_name, "user_id": user_id},
        on_conflict='ON CONFLICT ("group_name", "user_id") DO NOTHING',
        returning=COLUMNS["group_members"])
    return await _execute_returning(unit, sql, *args)


async def create_group(*, name: str, created_at: int,
                       unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "groups", {"name": name, "created_at": created_at},
        on_conflict='ON CONFLICT ("name") DO NOTHING',
        returning=COLUMNS["groups"])
    return await _execute_returning(unit, sql, *args)


async def list_group_members(group_name: str, *,
                             unit: Unit | None = None) -> list[Row]:
    return await _fetch(
        unit,
        f'SELECT {select_list("group_members")} FROM core."group_members" '
        'WHERE "group_name" = $1 ORDER BY "user_id"',
        group_name)


# ---------------------------------------------------------------------------
# measured prompt overhead
# ---------------------------------------------------------------------------

async def record_overhead(
    *, mind_id: str, harness: str, model: str, surface: str, tokens: int,
    observed_at: float, unit: Unit | None = None,
) -> Row:
    sql, args = _sql.insert(
        "context_overhead",
        {"mind_id": mind_id, "harness": harness, "model": model,
         "surface": surface, "tokens": tokens, "observed_at": observed_at},
        # The lowest reading wins, because the figure is the *floor* the
        # prompt costs and a later higher reading is a turn that carried
        # more, not a cheaper overhead. A plain overwrite would quietly turn
        # a measured minimum into last-write-wins.
        on_conflict=('ON CONFLICT ("mind_id", "harness", "model", "surface") '
                     'DO UPDATE SET '
                     '"tokens" = LEAST(core."context_overhead"."tokens", '
                     'EXCLUDED."tokens"), '
                     '"observed_at" = CASE WHEN EXCLUDED."tokens" < '
                     'core."context_overhead"."tokens" '
                     'THEN EXCLUDED."observed_at" '
                     'ELSE core."context_overhead"."observed_at" END'),
        returning=COLUMNS["context_overhead"])
    return await _execute_returning(unit, sql, *args)


async def get_overhead(*, mind_id: str, harness: str, model: str,
                       surface: str,
                       unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("context_overhead")} '
        'FROM core."context_overhead" '
        'WHERE "mind_id" = $1 AND "harness" = $2 AND "model" = $3 '
        'AND "surface" = $4',
        mind_id, harness, model, surface)


# ---------------------------------------------------------------------------
# What the gateway does that the first draft of this module could not express
# ---------------------------------------------------------------------------
#
# Each of these is a real call site in comms, found by reading it rather than
# by imagining what a data layer ought to offer. Without them a caller has no
# choice but to reach past this module, which makes "no service writes SQL"
# a slogan.

async def credential_for_token(token_sha: str, *,
                               unit: Unit | None = None) -> Row | None:
    """The live credential matching this bearer digest, or None.

    The per-request authentication read. Revoked credentials are excluded
    here rather than by the caller, because a credential that authenticates
    after it was revoked is the whole failure this column exists to prevent.
    The digest column is private by declaration, so it is named explicitly:
    the caller already has the digest, and the row it needs back is who the
    digest belongs to.
    """
    return await _fetch_one(
        unit,
        f'SELECT {select_list("credentials", tuple(c for c in COLUMNS["credentials"] if c != "token_sha"))} '
        'FROM core."credentials" '
        'WHERE "token_sha" = $1 AND "revoked_at" IS NULL',
        token_sha)


async def list_live_credentials(*, unit: Unit | None = None) -> list[Row]:
    """Every credential that has not been revoked."""
    return await _fetch(
        unit,
        f'SELECT {select_list("credentials")} FROM core."credentials" '
        'WHERE "revoked_at" IS NULL ORDER BY "holder"')


async def get_mind_by_id_or_name(value: str, *,
                                 unit: Unit | None = None) -> Row | None:
    """A mind by its identity or by its name, case-insensitively.

    The gateway resolves a mind from whatever a caller typed, and matches
    `lower(name)` when it does. A case-exact lookup answers None for a mind
    that is plainly there.
    """
    return await _fetch_one(
        unit,
        f'SELECT {select_list("minds")} FROM core."minds" '
        'WHERE "mind_id" = $1 OR lower("name") = lower($1)',
        value)


async def mind_session_token(mind_id: str, *,
                             unit: Unit | None = None) -> str | None:
    """The credential the gateway authenticates to this mind with.

    Its own operation, asking for one column, because the token is withheld
    from every listing on purpose: those answer to the service token every
    surface bot in the hive holds. A caller that needs it says so.
    """
    row = await _fetch_one(
        unit,
        'SELECT "session_token" FROM core."minds" WHERE "mind_id" = $1',
        mind_id)
    return None if row is None else row["session_token"]


async def reconcile_session_status(
    *, was: str, now: str, unit: Unit | None = None,
) -> Row:
    """Move every session in one status to another, and say how many moved.

    The boot reconciliation: a gateway that died with conversations running
    finds them still marked running, and nothing is answering for them. A
    write keyed to one id cannot express it, which is why it is its own
    operation rather than a parameter on `update_session`.
    """
    return await _execute(
        unit,
        'UPDATE core."sessions" SET "status" = $2 WHERE "status" = $1',
        was, now)


async def backfill_session_harness(
    *, mind_id: str, harness: str, unit: Unit | None = None,
) -> Row:
    """Give a mind's sessions a harness where none was recorded."""
    return await _execute(
        unit,
        'UPDATE core."sessions" SET "harness" = $2 '
        'WHERE "mind_id" = $1 AND "harness" IS NULL',
        mind_id, harness)


async def find_session_by_prefix(prefix: str, *,
                                 unit: Unit | None = None) -> list[Row]:
    """Sessions whose id starts with this, for the short ids people type."""
    return await _fetch(
        unit,
        f'SELECT {select_list("sessions")} FROM core."sessions" '
        'WHERE "id" LIKE $1 || \'%\' ORDER BY "last_active" DESC, "id" DESC',
        prefix)


async def pending_resends(*, statuses_excluded: Sequence[str],
                          unit: Unit | None = None) -> list[Row]:
    """Queued messages nobody has dispatched yet.

    Needs the resend columns, which every ordinary read withholds, so this
    operation asks for them by name and exists separately for that reason.
    """
    return await _fetch(
        unit,
        f'SELECT {select_list("sessions", COLUMNS["sessions"])} '
        'FROM core."sessions" '
        'WHERE "resend_content" IS NOT NULL '
        'AND "resend_dispatched_at" IS NULL '
        'AND NOT ("status" = ANY($1)) '
        'ORDER BY "last_active" ASC, "id" ASC',
        list(statuses_excluded))


async def delete_turn(turn_id: int, *, unit: Unit | None = None) -> Row:
    """Drop one turn by the identity its insert handed back.

    `record_turn` returns that identity precisely so this is possible: an
    overflowed turn has to take the un-sent user message out of the ledger
    again.
    """
    return await _execute(
        unit, 'DELETE FROM core."session_turns" WHERE "id" = $1', turn_id)


async def delete_turns(session_id: str, *, unit: Unit | None = None) -> Row:
    """Drop a session's whole ledger without dropping the session."""
    return await _execute(
        unit,
        'DELETE FROM core."session_turns" WHERE "session_id" = $1',
        session_id)


async def count_turns(session_id: str, *, roles: Sequence[str] | None = None,
                      since: float | None = None,
                      unit: Unit | None = None) -> int:
    """How many turns a session has, optionally by role and since a moment."""
    args: list[Any] = [session_id]
    sql = ('SELECT count(*) AS "turns" FROM core."session_turns" '
           'WHERE "session_id" = $1')
    if roles is not None:
        args.append(list(roles))
        sql += f' AND "role" = ANY(${len(args)})'
    if since is not None:
        args.append(since)
        sql += f' AND "created_at" >= ${len(args)}'
    row = await _fetch_one(unit, sql, *args)
    return 0 if row is None else int(row["turns"])


async def bindings_for_session(session_id: str, *,
                               unit: Unit | None = None) -> list[Row]:
    """Every surface currently pointed at this session."""
    return await _fetch(
        unit,
        f'SELECT {select_list("active_sessions")} '
        'FROM core."active_sessions" WHERE "session_id" = $1 '
        'ORDER BY "client_type", "client_ref"',
        session_id)


async def unbind_session(session_id: str, *,
                         unit: Unit | None = None) -> Row:
    """Release every surface holding this session, and say how many."""
    return await _execute(
        unit,
        'DELETE FROM core."active_sessions" WHERE "session_id" = $1',
        session_id)


async def claim_if(
    *, mind_id: str, surface: str, expected: dict[str, Any],
    unit: Unit | None = None, **fields: Any,
) -> Row:
    """Write a claim only while the conditions still hold.

    The conditional upsert the surface-claim module is built on — taking an
    unclaimed bot, issuing a phrase only while nobody owns it. An
    unconditional write expresses none of it, and an unconditional write is
    how a bot changes hands in silence.
    """
    if not expected:
        raise UnsettableColumn(
            "a conditional claim write with nothing expected is an "
            "unconditional one that reports it won. Name what has to still "
            "hold, or call upsert_claim.")
    values = _sql.passed(fields)
    clause, extra_args = _sql.where(
        "surface_claims", expected, start=len(values) + 3)
    sql, args = _sql.update(
        "surface_claims", values, {"mind_id": mind_id, "surface": surface},
        extra_where=clause)
    return await _execute(unit, sql, *args, *extra_args)


async def next_message_number(conversation_id: str, *,
                              unit: Unit | None = None) -> int:
    """The number the next message in this conversation takes.

    Read inside the same unit as the insert that uses it, or two concurrent
    deliveries derive the same number and the second hits the uniqueness on
    (conversation_id, message_number).
    """
    row = await _fetch_one(
        unit,
        'SELECT COALESCE(max("message_number"), 0) + 1 AS "next" '
        'FROM core."messages" WHERE "conversation_id" = $1',
        conversation_id)
    return 1 if row is None else int(row["next"])


async def get_conversation(conversation_id: str, *,
                           unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("conversations")} FROM core."conversations" '
        'WHERE "id" = $1',
        conversation_id)


async def session_memory_for_conversation(
    conversation_id: str, *, unit: Unit | None = None,
) -> list[Row]:
    """Carried-forward memory stamped with this conversation."""
    return await _fetch(
        unit,
        f'SELECT {select_list("session_memory")} FROM core."session_memory" '
        'WHERE "conversation_id" = $1 '
        'ORDER BY "created_at" DESC, "id" DESC',
        conversation_id)


#: What a user update may set. Not the identity, not the creation stamp.
USER_SETTABLE = frozenset({
    "password_hash", "disabled_at", "must_change", "email",
    "domain_account", "session_version",
})


async def update_user(user_id: int, *, unit: Unit | None = None,
                      **fields: Any) -> Row:
    """Change a person's record: a password, a lockout, a session bump."""
    sql, args = _sql.update(
        "users", _sql.passed(fields), {"id": user_id},
        settable=USER_SETTABLE)
    return await _execute(unit, sql, *args)


async def list_groups(*, unit: Unit | None = None) -> list[Row]:
    return await _fetch(
        unit,
        f'SELECT {select_list("groups")} FROM core."groups" ORDER BY "name"')


async def remove_group_member(*, group_name: str, user_id: int,
                              unit: Unit | None = None) -> Row:
    return await _execute(
        unit,
        'DELETE FROM core."group_members" '
        'WHERE "group_name" = $1 AND "user_id" = $2',
        group_name, user_id)


async def revoke_secret_scope(*, mind_name: str, secret_key: str,
                              unit: Unit | None = None) -> Row:
    return await _execute(
        unit,
        'DELETE FROM core."secret_scopes" '
        'WHERE "mind_name" = $1 AND "secret_key" = $2',
        mind_name, secret_key)


async def get_auth_setting(key: str, *,
                           unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("auth_settings")} FROM core."auth_settings" '
        'WHERE "key" = $1',
        key)


async def put_auth_setting(key: str, value: str, *,
                           unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "auth_settings", {"key": key, "value": value},
        on_conflict=('ON CONFLICT ("key") DO UPDATE SET '
                     '"value" = EXCLUDED."value"'),
        returning=COLUMNS["auth_settings"])
    return await _execute_returning(unit, sql, *args)


async def put_auth_setting_if_absent(key: str, value: str, *,
                                     unit: Unit | None = None) -> Row:
    """Store a setting unless it is already there, and report what holds.

    One statement rather than a read and a write, because the one caller is
    the console minting its cookie-signing key on first use: two workers
    reaching an empty install at once would each generate a key and each
    write it, and whichever landed second would sign every later cookie and
    invalidate the ones the other had already issued — which presents as the
    whole install being signed out.

    The `DO UPDATE` is a no-op write of the value already there, which is how
    one statement both inserts and reports. `DO NOTHING` would return no row
    at all when the key exists, leaving the caller to read it in a second
    statement — the race this exists to close.
    """
    sql, args = _sql.insert(
        "auth_settings", {"key": key, "value": value},
        on_conflict=('ON CONFLICT ("key") DO UPDATE SET '
                     '"value" = core."auth_settings"."value"'),
        returning=COLUMNS["auth_settings"])
    return await _execute_returning(unit, sql, *args)


async def get_sso_binding(*, provider: str, subject: str,
                          unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit,
        f'SELECT {select_list("sso_bindings")} FROM core."sso_bindings" '
        'WHERE "provider" = $1 AND "subject" = $2',
        provider, subject)


async def put_sso_binding(*, provider: str, subject: str, username: str,
                          created_at: int,
                          unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "sso_bindings",
        {"provider": provider, "subject": subject, "username": username,
         "created_at": created_at},
        on_conflict=('ON CONFLICT ("provider", "subject") DO UPDATE SET '
                     '"username" = EXCLUDED."username"'),
        returning=COLUMNS["sso_bindings"])
    return await _execute_returning(unit, sql, *args)


async def sso_usernames(provider: str, *,
                        unit: Unit | None = None) -> list[str]:
    """Who has an outside identity bound for a provider, by account name.

    The console's accounts page draws a badge from this, so what it wants is
    the set of names rather than the subjects behind them - and the subject
    is the opaque identifier the identity provider issued, which has no
    business on a page.
    """
    rows = await _fetch(
        unit,
        'SELECT DISTINCT "username" FROM core."sso_bindings" '
        'WHERE "provider" = $1 ORDER BY "username"',
        provider)
    return [row["username"] for row in rows]


async def delete_sso_binding(*, provider: str, username: str,
                             unit: Unit | None = None) -> Row:
    """Unbind an outside identity from an account.

    By account rather than by subject, which is what the console's route
    asks for: the operator knows whose Microsoft sign-in they are detaching
    and does not know the subject. The row count says whether there was one,
    so the route can answer "nothing to unbind" rather than reporting a
    success over an account that never had a binding.
    """
    return await _execute(
        unit,
        'DELETE FROM core."sso_bindings" '
        'WHERE "provider" = $1 AND "username" = $2',
        provider, username)


async def least_overhead(*, mind_id: str, harness: str,
                         unit: Unit | None = None) -> int | None:
    """The lowest overhead measured for this mind on this harness.

    Across every model and surface, which is what the gateway reads when it
    has no reading for the exact combination in front of it.
    """
    row = await _fetch_one(
        unit,
        'SELECT min("tokens") AS "tokens" FROM core."context_overhead" '
        'WHERE "mind_id" = $1 AND "harness" = $2',
        mind_id, harness)
    return None if row is None or row["tokens"] is None else int(row["tokens"])
