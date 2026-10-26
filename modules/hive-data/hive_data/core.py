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
from .rows import UNSET, as_row, as_rows, row_count, written
from .schema import COLUMNS, select_list
from .unit import Unit

Row = dict[str, Any]


async def _rows(connection: Any, sql: str, *args: Any) -> list[Row]:
    return as_rows(await connection.fetch(sql, *args))


async def fetch(unit: Unit | None, sql: str, *args: Any) -> list[Row]:
    """Every matching row, as plain dicts. Empty list when nothing matches."""
    if unit is not None:
        return await unit.fetch(sql, *args)
    return await pool.run(lambda c: _rows(c, sql, *args))


async def fetch_one(unit: Unit | None, sql: str, *args: Any) -> Row | None:
    """The first matching row, or None.

    None rather than an empty dict: an empty dict is a row with no columns,
    and a caller testing it for truth cannot tell the two apart.
    """
    async def _one(connection: Any) -> Row | None:
        return as_row(await connection.fetchrow(sql, *args))

    if unit is not None:
        return await unit.fetch_one(sql, *args)
    return await pool.run(_one)


async def execute(unit: Unit | None, sql: str, *args: Any) -> Row:
    """Run a write and report how many rows it changed."""
    async def _run(connection: Any) -> Row:
        return written(row_count(await connection.execute(sql, *args)))

    if unit is not None:
        return await unit.execute(sql, *args)
    return await pool.run(_run)


async def execute_returning(unit: Unit | None, sql: str, *args: Any) -> Row:
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
    return await fetch_one(
        unit,
        f'SELECT {select_list("minds")} FROM core."minds" '
        'WHERE "mind_id" = $1',
        mind_id)


async def get_mind_by_name(name: str, *,
                           unit: Unit | None = None) -> Row | None:
    return await fetch_one(
        unit,
        f'SELECT {select_list("minds")} FROM core."minds" WHERE "name" = $1',
        name)


async def list_minds(*, unit: Unit | None = None) -> list[Row]:
    return await fetch(
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
    updatable = [c for c in values if c != "name"]
    assignments = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in updatable)
    sql, args = _sql.insert(
        "minds", values,
        on_conflict=f'ON CONFLICT ("name") DO UPDATE SET {assignments}',
        returning=COLUMNS["minds"])
    return await execute_returning(unit, sql, *args)


async def touch_mind(mind_id: str, last_seen: float, *,
                     unit: Unit | None = None) -> Row:
    sql, args = _sql.update(
        "minds", {"last_seen": last_seen}, {"mind_id": mind_id},
        settable=frozenset({"last_seen"}))
    return await execute(unit, sql, *args)


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
    return await execute_returning(unit, sql, *args)


async def get_session(session_id: str, *, private: bool = False,
                      unit: Unit | None = None) -> Row | None:
    """One session row.

    `private` has to be asked for. The carry-forward and resend columns hold
    a composed system prompt and a queued user message, and the listings that
    read sessions answer to the service token every surface bot holds.
    """
    columns = COLUMNS["sessions"] if private else None
    return await fetch_one(
        unit,
        f'SELECT {select_list("sessions", columns)} FROM core."sessions" '
        'WHERE "id" = $1',
        session_id)


async def get_session_by_conversation(
    conversation_id: str, *, unit: Unit | None = None,
) -> Row | None:
    return await fetch_one(
        unit,
        f'SELECT {select_list("sessions")} FROM core."sessions" '
        'WHERE "conversation_id" = $1',
        conversation_id)


async def list_sessions(
    *, surfaces: Sequence[str] | None = None, status: str | None = None,
    mind_id: str | None = None, limit: int | None = None,
    unit: Unit | None = None,
) -> list[Row]:
    """Sessions, filtered by surface, status and mind.

    The filters are separate parameters rather than a condition string: a
    caller that could pass SQL would pass SQL.
    """
    conditions: list[str] = []
    args: list[Any] = []
    if surfaces is not None:
        args.append(list(surfaces))
        conditions.append(f'"surface" = ANY(${len(args)})')
    if status is not None:
        args.append(status)
        conditions.append(f'"status" = ${len(args)}')
    if mind_id is not None:
        args.append(mind_id)
        conditions.append(f'"mind_id" = ${len(args)}')
    sql = f'SELECT {select_list("sessions")} FROM core."sessions"'
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += ' ORDER BY "last_active" DESC, "id" DESC'
    if limit is not None:
        args.append(limit)
        sql += f" LIMIT ${len(args)}"
    return await fetch(unit, sql, *args)


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
    return await execute(unit, sql, *args)


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
    values = _sql.passed(fields)
    clause, extra_args = _sql.where(
        "sessions", expected, start=len(values) + 2)
    sql, args = _sql.update(
        "sessions", values, {"id": session_id},
        settable=SESSION_SETTABLE, extra_where=clause)
    return await execute(unit, sql, *args, *extra_args)


async def delete_session(session_id: str, *, unit: Unit | None = None) -> Row:
    """Delete a session. Its turns and surface binding go with it.

    Both are declared ON DELETE CASCADE in the applied schema, because a
    turn ledger and a surface binding are parts of their session rather than
    things that outlive it.
    """
    return await execute(
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
    return await execute_returning(unit, sql, *args)


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
    return await fetch(unit, sql, *args)


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
    return await fetch_one(unit, sql, *args)


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
    return await execute_returning(unit, sql, *args)


async def bound_session(client_type: str, client_ref: str, *,
                        unit: Unit | None = None) -> Row | None:
    return await fetch_one(
        unit,
        f'SELECT {select_list("active_sessions")} '
        'FROM core."active_sessions" '
        'WHERE "client_type" = $1 AND "client_ref" = $2',
        client_type, client_ref)


async def unbind_surface(client_type: str, client_ref: str, *,
                         unit: Unit | None = None) -> Row:
    return await execute(
        unit,
        'DELETE FROM core."active_sessions" '
        'WHERE "client_type" = $1 AND "client_ref" = $2',
        client_type, client_ref)


# ---------------------------------------------------------------------------
# who a mind's bot answers
# ---------------------------------------------------------------------------

async def get_claim(mind_id: str, surface: str, *,
                    unit: Unit | None = None) -> Row | None:
    return await fetch_one(
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
    return await execute(
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
    return await execute_returning(
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
    values.setdefault("phrase_attempts", 0)
    updatable = [c for c in values if c not in ("mind_id", "surface")]
    assignments = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in updatable)
    sql, args = _sql.insert(
        "surface_claims", values,
        on_conflict=(f'ON CONFLICT ("mind_id", "surface") DO UPDATE SET '
                     f'{assignments}' if updatable else
                     'ON CONFLICT ("mind_id", "surface") DO NOTHING'),
        returning=COLUMNS["surface_claims"])
    return await execute_returning(unit, sql, *args)


# ---------------------------------------------------------------------------
# broker conversations and their messages
# ---------------------------------------------------------------------------

async def create_conversation(*, id: str, created_at: float,
                              unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "conversations", {"id": id, "created_at": created_at},
        on_conflict='ON CONFLICT ("id") DO NOTHING', returning=("id",))
    return await execute_returning(unit, sql, *args)


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
    return await execute_returning(unit, sql, *args)


async def list_messages(conversation_id: str, *, limit: int | None = None,
                        unit: Unit | None = None) -> list[Row]:
    args: list[Any] = [conversation_id]
    sql = (f'SELECT {select_list("messages")} FROM core."messages" '
           'WHERE "conversation_id" = $1 '
           'ORDER BY "message_number" ASC, "id" ASC')
    if limit is not None:
        args.append(limit)
        sql += f" LIMIT ${len(args)}"
    return await fetch(unit, sql, *args)


async def update_message(message_id: str, *, unit: Unit | None = None,
                         **fields: Any) -> Row:
    sql, args = _sql.update(
        "messages", _sql.passed(fields), {"id": message_id},
        settable=frozenset({
            "content", "rolling_summary", "metadata", "status",
            "recipient_session_id", "response_error"}))
    return await execute(unit, sql, *args)


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
    return await execute_returning(unit, sql, *args)


async def latest_session_memory(
    *, mind_id: str, client_ref: str, unit: Unit | None = None,
) -> Row | None:
    """The most recent carried-forward memory for this conversation.

    Read by mind and client reference, never by session: the
    `conversation_id` column is a provenance stamp rather than a reference,
    which is exactly what its old name hid.
    """
    return await fetch_one(
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
    return await fetch_one(
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
    return await execute_returning(unit, sql, *args)


async def revoke_credential(holder: str, revoked_at: float, *,
                            unit: Unit | None = None) -> Row:
    sql, args = _sql.update(
        "credentials", {"revoked_at": revoked_at}, {"holder": holder},
        settable=frozenset({"revoked_at"}))
    return await execute(unit, sql, *args)


async def get_setting(key: str, *, unit: Unit | None = None) -> Row | None:
    return await fetch_one(
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
    return await execute_returning(unit, sql, *args)


async def grant_secret_scope(*, mind_name: str, secret_key: str,
                             granted_at: float,
                             unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "secret_scopes",
        {"mind_name": mind_name, "secret_key": secret_key,
         "granted_at": granted_at},
        on_conflict='ON CONFLICT ("mind_name", "secret_key") DO NOTHING',
        returning=COLUMNS["secret_scopes"])
    return await execute_returning(unit, sql, *args)


async def list_secret_scopes(mind_name: str, *,
                             unit: Unit | None = None) -> list[Row]:
    return await fetch(
        unit,
        f'SELECT {select_list("secret_scopes")} FROM core."secret_scopes" '
        'WHERE "mind_name" = $1 ORDER BY "secret_key"',
        mind_name)


async def get_user(username: str, *, unit: Unit | None = None) -> Row | None:
    return await fetch_one(
        unit,
        f'SELECT {select_list("users")} FROM core."users" '
        'WHERE "username" = $1',
        username)


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
    return await execute_returning(unit, sql, *args)


async def add_group_member(*, group_name: str, user_id: int,
                           unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "group_members", {"group_name": group_name, "user_id": user_id},
        on_conflict='ON CONFLICT ("group_name", "user_id") DO NOTHING',
        returning=COLUMNS["group_members"])
    return await execute_returning(unit, sql, *args)


async def create_group(*, name: str, created_at: int,
                       unit: Unit | None = None) -> Row:
    sql, args = _sql.insert(
        "groups", {"name": name, "created_at": created_at},
        on_conflict='ON CONFLICT ("name") DO NOTHING',
        returning=COLUMNS["groups"])
    return await execute_returning(unit, sql, *args)


async def list_group_members(group_name: str, *,
                             unit: Unit | None = None) -> list[Row]:
    return await fetch(
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
        on_conflict=('ON CONFLICT ("mind_id", "harness", "model", "surface") '
                     'DO UPDATE SET "tokens" = EXCLUDED."tokens", '
                     '"observed_at" = EXCLUDED."observed_at"'),
        returning=COLUMNS["context_overhead"])
    return await execute_returning(unit, sql, *args)


async def get_overhead(*, mind_id: str, harness: str, model: str,
                       surface: str,
                       unit: Unit | None = None) -> Row | None:
    return await fetch_one(
        unit,
        f'SELECT {select_list("context_overhead")} '
        'FROM core."context_overhead" '
        'WHERE "mind_id" = $1 AND "harness" = $2 AND "model" = $3 '
        'AND "surface" = $4',
        mind_id, harness, model, surface)
