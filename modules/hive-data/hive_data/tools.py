"""The named operations over the `tools` schema.

hive-tools is the hive's hands: gmail, calendar, the browser, docker. What it
keeps in a database is small and all of it is about permission — who may call
a tool, which calls need a person to say yes first, and which have been said
yes to already.

Two things here are not simply ported. The expiry of a pending approval was
computed in Python and then written back as a second statement, so a request
could be decided and expire between the read and the write; `decide_request`
puts the whole rule in one statement and reports whether it changed anything.
And `tools.users` is a different registry from `core.users` - hive-tools'
own logins, not the console's people - which is why every table here is
declared `schema.table` rather than by the bare name `core` already spoke
for.

Every operation takes an optional `unit` and behaves as the `core` ones do:
given one it runs inside that caller's transaction, given none it runs on its
own connection and commits by itself.
"""

from __future__ import annotations

from typing import Any

from . import _sql
from .core import _execute, _execute_returning, _fetch, _fetch_one
from .errors import ValueRefused
from .schema import COLUMNS, qualified, select_list
from .unit import Unit

Row = dict[str, Any]

#: The newest requests the approvals page renders, and how many. Carried from
#: hive-tools' own listing rather than widened: the page has no paging, so a
#: larger number is a longer page and not more information.
QUEUE_LIMIT = 50

TOKENS = "tools.api_tokens"
REQUESTS = "tools.hitl_requests"
GRANTS = "tools.sudo_grants"
SETTINGS = "tools.tool_hitl_settings"
USERS = "tools.users"

#: The one status a request can be decided from. Written here rather than at
#: each call site because it is the guard in `decide_request`'s statement as
#: well as the value `create_request` writes.
PENDING = "pending"


# ---------------------------------------------------------------- approvals

async def create_request(request_id: str, tool_name: str, caller_mind: str,
                         params_json: str, created_at: int, expires_at: int,
                         *, status: str = PENDING,
                         tool_method: str | None = None,
                         tool_path: str | None = None,
                         tool_body: str | None = None,
                         unit: Unit | None = None) -> Row:
    """Record a call waiting on a person."""
    sql, args = _sql.insert(REQUESTS, {
        "id": request_id, "tool_name": tool_name, "caller_mind": caller_mind,
        "params_json": params_json, "status": status,
        "created_at": created_at, "expires_at": expires_at,
        "tool_method": tool_method, "tool_path": tool_path,
        "tool_body": tool_body,
    })
    return await _execute(unit, sql, *args)


async def request(request_id: str, *, unit: Unit | None = None) -> Row | None:
    """One request by id, or None when there is no such request.

    None rather than an empty dict, for the reason the `core` reads give: an
    empty dict is a row with no columns, and a caller testing it for truth
    cannot tell the two apart.
    """
    return await _fetch_one(
        unit,
        f"SELECT {select_list(REQUESTS)} FROM {qualified(REQUESTS)} "
        "WHERE id = $1", request_id)


async def request_payload(request_id: str, *,
                          unit: Unit | None = None) -> Row | None:
    """A request with the call it captured, for replaying an approved one.

    The one read that names the private columns. Everything else about a
    request is on `request`; the body is here, so a listing cannot pick it
    up by accident.
    """
    columns = ("id", "tool_name", "caller_mind", "status", "params_json",
               "tool_method", "tool_path", "tool_body")
    return await _fetch_one(
        unit,
        f"SELECT {select_list(REQUESTS, columns)} "
        f"FROM {qualified(REQUESTS)} WHERE id = $1", request_id)


async def recent_requests(*, limit: int = QUEUE_LIMIT,
                          unit: Unit | None = None) -> list[Row]:
    """The approvals queue as the page shows it: newest first, capped.

    Every status, not only the pending ones. The page is a log of what has
    been asked as much as a list of what is waiting, and a queue that hid
    yesterday's denials would read as though nothing had ever been refused.

    A limit is required to be a cap. `LIMIT NULL` is no limit at all, and
    `None` is the natural spelling of "no cap" coming off an HTTP query
    parameter - which would quietly return every request in the table from
    a route that reads as paged.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueRefused(
            f"a queue limit of {limit!r} is not a cap. LIMIT NULL is no "
            "limit, and this listing is the whole approvals table when it "
            "is not capped. Pass a positive count, or leave it out for "
            f"{QUEUE_LIMIT}.")
    return await _fetch(
        unit,
        f"SELECT {select_list(REQUESTS)} FROM {qualified(REQUESTS)} "
        "ORDER BY created_at DESC, id DESC LIMIT $1", limit)


async def pending_request(request_id: str, now: int, *,
                          unit: Unit | None = None) -> Row | None:
    """A request still waiting on a person at `now`, or None.

    `now` is passed in rather than read off the clock here, because the
    caller has already timestamped the decision it is about to make and two
    readings of the clock inside one decision is how a request expires
    halfway through being approved.
    """
    return await _fetch_one(
        unit,
        f"SELECT {select_list(REQUESTS)} FROM {qualified(REQUESTS)} "
        "WHERE id = $1 AND status = $2 AND expires_at >= $3",
        request_id, PENDING, now)


async def decide_request(request_id: str, status: str, decided_at: int,
                         now: int, *, unit: Unit | None = None) -> Row:
    """Approve or deny a request, if it is still there to be decided.

    The guard is in the statement. A request that has already been decided,
    or whose expiry has passed, reports zero rows changed rather than being
    overwritten - so two people clicking approve and deny at the same moment
    produce one decision and one caller told it lost.

    The winner is handed the call it just approved. The replay needs the
    method, the path and the body, and re-reading them afterwards is a
    second statement the loser also runs - so they come back with the
    decision rather than being fetched again.
    """
    if status == PENDING:
        raise ValueRefused(
            f"{PENDING!r} is not a decision. Deciding a request back to "
            "pending stamps `decided_at`, reports a row changed, and leaves "
            "the request decidable again, so a caller would be told it won "
            "an arbitration that is still open.")
    values = {"status": status, "decided_at": decided_at}
    clause, extra_args = _sql.where(
        REQUESTS, {"status": PENDING}, start=len(values) + 2)
    sql, args = _sql.update(
        REQUESTS, values, {"id": request_id},
        extra_where=f'{clause} AND "expires_at" >= ${len(values) + 3}',
        returning=("id", "tool_name", "caller_mind", "params_json",
                   "tool_method", "tool_path", "tool_body"))
    return await _execute_returning(unit, sql, *args, *extra_args, now)


#: The one status an outcome can be recorded against. A denied request ran
#: nothing and an expired one was never decided, so neither has an outcome.
APPROVED = "approved"


async def record_outcome(request_id: str, ok: bool, status: int | None,
                         body: str, at: int, *,
                         unit: Unit | None = None) -> Row:
    """Record what an approved request's replay did, once.

    Guarded in the statement on the request being approved and having no
    outcome yet, so a second replay of one approval - there should never be
    one - cannot overwrite the first answer, and a denial can never be given
    a result it did not produce. Reports how many rows it changed.
    """
    values = {"outcome_ok": bool(ok), "outcome_status": status,
              "outcome_body": body, "outcome_at": at}
    clause, extra_args = _sql.where(
        REQUESTS, {"status": APPROVED, "outcome_at": None},
        start=len(values) + 2)
    sql, args = _sql.update(REQUESTS, values, {"id": request_id},
                            extra_where=clause)
    return await _execute(unit, sql, *args, *extra_args)


async def request_outcome(request_id: str, *,
                          unit: Unit | None = None) -> Row | None:
    """A request's replay outcome, body included, or None for no request.

    The one read naming `outcome_body`, which is private so the approvals
    listing never carries it. The other three outcome columns are on
    `request` as well; NULL `outcome_at` means none has landed.
    """
    columns = ("id", "outcome_ok", "outcome_status", "outcome_body",
               "outcome_at")
    return await _fetch_one(
        unit,
        f"SELECT {select_list(REQUESTS, columns)} "
        f"FROM {qualified(REQUESTS)} WHERE id = $1", request_id)


async def extend_request(request_id: str, expires_at: int, *,
                         unit: Unit | None = None) -> Row:
    """Give a request still waiting on a person longer to wait.

    Guarded on pending for the same reason the decision is: moving the
    expiry of a request that was already denied makes a closed decision
    look open again, and reports a row changed while doing it.
    """
    values = {"expires_at": expires_at}
    clause, extra_args = _sql.where(
        REQUESTS, {"status": PENDING}, start=len(values) + 2)
    sql, args = _sql.update(REQUESTS, values, {"id": request_id},
                            extra_where=clause)
    return await _execute(unit, sql, *args, *extra_args)


# ------------------------------------------------------------------- grants

async def grant_sudo(tool_name: str, caller_mind: str, granted_at: int,
                     expires_at: int, *, unit: Unit | None = None) -> Row:
    """Record that a mind may call a tool without asking, until a time."""
    sql, args = _sql.insert(GRANTS, {
        "tool_name": tool_name, "caller_mind": caller_mind,
        "granted_at": granted_at, "expires_at": expires_at,
    }, returning=("id",))
    return await _execute_returning(unit, sql, *args)


async def active_grant(tool_name: str, caller_mind: str, now: int, *,
                       unit: Unit | None = None) -> Row | None:
    """A grant still live at `now`, or None.

    Strictly after `now`, which is what hive-tools asks today: a grant whose
    expiry is this instant has run out.
    """
    return await _fetch_one(
        unit,
        f"SELECT {select_list(GRANTS)} FROM {qualified(GRANTS)} "
        "WHERE tool_name = $1 AND caller_mind = $2 AND expires_at > $3",
        tool_name, caller_mind, now)


# ----------------------------------------------------------------- settings

async def hitl_settings(*, unit: Unit | None = None) -> list[Row]:
    """Every tool's approval mode, in the order the settings page lists."""
    return await _fetch(
        unit,
        f"SELECT {select_list(SETTINGS)} FROM {qualified(SETTINGS)} "
        "ORDER BY tool_name")


async def hitl_setting(tool_name: str, *,
                       unit: Unit | None = None) -> Row | None:
    """One tool's approval mode, or None when it has never been set."""
    return await _fetch_one(
        unit,
        f"SELECT {select_list(SETTINGS)} FROM {qualified(SETTINGS)} "
        "WHERE tool_name = $1", tool_name)


async def seed_hitl_setting(tool_name: str, mode: str,
                            sudo_timeout_minutes: int, *,
                            unit: Unit | None = None) -> Row:
    """Give a tool an approval mode only if it has none.

    hive-tools registers its tools on every boot and seeds the ones it has
    never seen. That write is deliberately insert-if-absent: an operator who
    turned a tool's gate off expects it to stay off across a restart, and a
    replacing write re-arms every gate in the table on the next deploy. The
    row count is how the caller knows which tools were new.
    """
    sql, args = _sql.insert(SETTINGS, {
        "tool_name": tool_name, "mode": mode,
        "sudo_timeout_minutes": sudo_timeout_minutes,
    }, on_conflict='ON CONFLICT ("tool_name") DO NOTHING')
    return await _execute(unit, sql, *args)


async def set_hitl_setting(tool_name: str, mode: str,
                           sudo_timeout_minutes: int, *,
                           unit: Unit | None = None) -> Row:
    """Set a tool's approval mode, replacing whatever it was."""
    sql, args = _sql.insert(SETTINGS, {
        "tool_name": tool_name, "mode": mode,
        "sudo_timeout_minutes": sudo_timeout_minutes,
    }, on_conflict=(
        'ON CONFLICT ("tool_name") DO UPDATE SET '
        '"mode" = excluded."mode", '
        '"sudo_timeout_minutes" = excluded."sudo_timeout_minutes"'))
    return await _execute(unit, sql, *args)


# ------------------------------------------------------------------- tokens

async def create_token(name: str, token_hash: str, created_at: int, *,
                       unit: Unit | None = None) -> Row:
    """Store a new API token by its digest."""
    sql, args = _sql.insert(TOKENS, {
        "name": name, "token_hash": token_hash, "created_at": created_at,
    }, returning=("id",))
    return await _execute_returning(unit, sql, *args)


async def tokens(*, unit: Unit | None = None) -> list[Row]:
    """Every token, newest first, without the digests.

    The digest is declared private: reading one is how a caller
    authenticates as somebody else, and no listing has ever needed it.
    """
    return await _fetch(
        unit,
        f"SELECT {select_list(TOKENS)} FROM {qualified(TOKENS)} "
        "ORDER BY created_at DESC, id DESC")


async def live_token(token_hash: str, *,
                     unit: Unit | None = None) -> Row | None:
    """The token a digest belongs to, while it is still valid."""
    return await _fetch_one(
        unit,
        f'SELECT "id", "name" FROM {qualified(TOKENS)} '
        "WHERE token_hash = $1 AND revoked_at IS NULL", token_hash)


async def record_token_use(token_id: int, at: int, *,
                           unit: Unit | None = None) -> Row:
    """Note when a token was last used."""
    sql, args = _sql.update(TOKENS, {"last_used_at": at}, {"id": token_id})
    return await _execute(unit, sql, *args)


async def revoke_token(token_id: int, at: int, *,
                       unit: Unit | None = None) -> Row:
    """Revoke a token, if it is not revoked already.

    The guard goes through `_sql.where`, which is the only thing that turns
    `None` into `IS NULL`. Spelled as an ordinary condition it becomes
    `revoked_at = NULL`, which is never true - so the statement matches
    nothing, reports zero rows, and the operator who clicked revoke watches
    the page reload while the bearer keeps authenticating.
    """
    values = {"revoked_at": at}
    clause, extra_args = _sql.where(
        TOKENS, {"revoked_at": None}, start=len(values) + 2)
    sql, args = _sql.update(TOKENS, values, {"id": token_id},
                            extra_where=clause)
    return await _execute(unit, sql, *args, *extra_args)


# -------------------------------------------------------------------- users

async def create_user(username: str, password_hash: str, is_admin: int,
                      created_at: int, *, unit: Unit | None = None) -> Row:
    """Add one of hive-tools' own logins."""
    sql, args = _sql.insert(USERS, {
        "username": username, "password_hash": password_hash,
        "is_admin": is_admin, "created_at": created_at,
    }, returning=("id",))
    return await _execute_returning(unit, sql, *args)


#: The login read names the private hash on purpose - checking a password is
#: the one caller that needs it - and takes the rest from the declaration
#: rather than restating it, so a column added to `tools.users` arrives here
#: instead of being silently absent from the one read that bypassed both
#: `COLUMNS` and `PRIVATE`.
def _login_columns() -> tuple[str, ...]:
    return COLUMNS[USERS]


async def user(username: str, *, unit: Unit | None = None) -> Row | None:
    """One login by name, with its hash, for checking a password."""
    return await _fetch_one(
        unit,
        f"SELECT {select_list(USERS, _login_columns())} "
        f"FROM {qualified(USERS)} WHERE username = $1", username)


async def user_by_id(user_id: int, *,
                     unit: Unit | None = None) -> Row | None:
    """One login by id, which is what a session cookie carries.

    Every authenticated page load resolves its cookie to a user id and reads
    the login back, so this is the hottest read in the schema.
    """
    return await _fetch_one(
        unit,
        f"SELECT {select_list(USERS, _login_columns())} "
        f"FROM {qualified(USERS)} WHERE id = $1", user_id)


async def count_users(*, unit: Unit | None = None) -> int:
    """How many logins there are.

    A count rather than the rows. The one caller asks at every boot to decide
    whether to seed the bootstrap admin, and answering it by reading the
    whole table back is a full scan and every password hash over the wire to
    answer a question about a number.
    """
    row = await _fetch_one(
        unit, f'SELECT count(*) AS "count" FROM {qualified(USERS)}')
    return 0 if row is None else int(row["count"])


async def users(*, unit: Unit | None = None) -> list[Row]:
    """Every login, without the hashes."""
    return await _fetch(
        unit,
        f"SELECT {select_list(USERS)} FROM {qualified(USERS)} "
        "ORDER BY username")


async def disable_user(username: str, at: int, *,
                       unit: Unit | None = None) -> Row:
    """Disable a login."""
    sql, args = _sql.update(USERS, {"disabled_at": at},
                            {"username": username})
    return await _execute(unit, sql, *args)
