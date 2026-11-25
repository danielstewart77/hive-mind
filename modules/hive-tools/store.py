"""Where hive-tools' data lives: the `tools` schema of the hive's Postgres.

Nothing else in hive-tools opens a database or writes SQL. Every route calls
a named operation here, and every operation is answered by the hive's one
Postgres through `hive_data`, at `HIVE_DATABASE_URL`. There is no other
store: the SQLite file this service used before the cutover is retired, and
with `HIVE_DATABASE_URL` unset the service refuses to start rather than
opening one — before anything at startup can read an empty store and seed
it.

The composite rules - when an approval has expired, whether a decision
grants sudo, what a replay needs - are not here. They live in `hitl.py`,
once, on top of these operations.
"""

from __future__ import annotations

import contextvars
import os
from typing import Any

import hive_data
from hive_data import core, tools

#: The variable naming the database. Without it there is nothing to open.
DSN_VAR = "HIVE_DATABASE_URL"

#: The newest approvals the queue page shows. The page has no paging, so a
#: larger number is a longer page rather than more information.
QUEUE_LIMIT = 50

Row = dict[str, Any]

_open = False
#: Whether this store was opened and then closed, as against never opened.
#: The two are different sentences: the first is a straggling background task
#: reaching a store the lifespan has already shut down, which is ordinary at
#: shutdown, and the second is a route running outside the app.
_was_open = False

#: The unit of work the current task is inside, if any. A context variable
#: rather than a parameter on thirty operations, so a call site reads the same
#: whether or not it is in a transaction.
_unit: contextvars.ContextVar[Any] = contextvars.ContextVar("unit",
                                                            default=None)


async def open() -> None:
    """Open the hive's Postgres, or refuse when none is named.

    Called from the application's lifespan rather than at import, because
    opening a pool is async and because a module that connected on import
    would do it again in every test that imported it.
    """
    global _open, _was_open
    if not os.environ.get(DSN_VAR, "").strip():
        raise RuntimeError(
            f"{DSN_VAR} is unset. hive-tools' data lives in the hive's "
            "Postgres and nowhere else — the SQLite file it once fell back "
            f"to is retired — so name the database in {DSN_VAR} and start "
            "it again.")
    await hive_data.connect()
    _open = True
    _was_open = True


async def close() -> None:
    global _open
    was, _open = _open, False
    if was:
        await hive_data.close(drain_timeout=5.0)


def _unopened() -> RuntimeError:
    """Why there is no store, said as the thing that actually happened.

    A store that was opened and closed is a straggling background task - the
    fire-and-forget replay, the training watcher - reaching a database after
    the lifespan shut it down, which is ordinary at shutdown. A store that
    was never opened is a route running outside the app. Folding the two
    together sends somebody hunting the second when it was the first.
    """
    if _was_open:
        return RuntimeError(
            "hive-tools' store is closed. The lifespan shut it down, so this "
            "is work that outlived the application - a background replay or a "
            "watcher still running after shutdown - rather than a route "
            "reaching a database too early.")
    return RuntimeError(
        "hive-tools' store was never opened. The application's lifespan "
        "opens it; a caller reaching a database before that is a route "
        "running outside the app.")


def _live() -> Any:
    """The `hive_data` unit this task is inside, or None; refused when closed."""
    if not _open:
        raise _unopened()
    return _unit.get()


class transaction:
    """Several operations, all of them or none.

    It exists because deciding an approval is not one write. The decision,
    the mode it reads and the sudo grant it may insert are three statements
    under one commit, and committed separately they leave an end state
    nobody can recover from: the request says approved, the call it
    authorised was never replayed, and because the decision is guarded on
    `pending` it can never be approved again.

    A class rather than an `@asynccontextmanager`, and the reason is the
    access layer refusing the generator version outright. `hive_data`
    refuses to open a unit of work inside an async generator, because a
    generator yields to its caller while holding one pooled connection
    inside an open transaction and a caller that abandons it - a closed
    browser tab on a streaming reply - leaves the transaction to unwind
    whenever the event loop's asyncgen finalizer reaches it. `__aenter__`
    and `__aexit__` are ordinary coroutines and there is no generator to
    finalize.

    The unit travels in a context variable, so the operations inside need no
    extra argument and every call site reads the same in or out of one. A
    task created inside a unit inherits it - `hive_data.Unit` refuses to be
    used from another task, which is the protection wanted rather than a
    limitation.
    """

    def __init__(self) -> None:
        self._scope: Any = None
        self._token: Any = None

    async def __aenter__(self) -> None:
        if _unit.get() is not None:
            raise RuntimeError(
                "hive-tools' store does not nest transactions. The inner "
                "block would end the outer one's work on its way out, so a "
                "failure after it would roll back nothing. Open one unit at "
                "the top of the operation.")
        if not _open:
            raise _unopened()
        self._scope = hive_data.transaction()
        self._token = _unit.set(await self._scope.__aenter__())
        return None

    async def __aexit__(self, kind: Any, value: Any, trace: Any) -> bool:
        # The context variable is reset before the commit or the rollback, so
        # the statement that ends the unit is not itself run as part of one.
        _unit.reset(self._token)
        return bool(await self._scope.__aexit__(kind, value, trace))


# -------------------------------------------------------------------- users

async def count_users() -> int:
    return await tools.count_users(unit=_live())


async def user_by_username(username: str) -> Row | None:
    return await tools.user(username, unit=_live())


async def user_by_id(user_id: int) -> Row | None:
    return await tools.user_by_id(user_id, unit=_live())


async def create_user(username: str, password_hash: str, is_admin: int,
                      created_at: int) -> Row | None:
    await tools.create_user(username, password_hash, is_admin, created_at,
                            unit=_live())
    return await user_by_username(username)


async def admin_usernames() -> set[str]:
    """Who signs in here as an admin: the person whose integrations - the
    Google account, the LinkedIn token, the phone - this hive-tools holds."""
    return {str(row["username"]).strip().lower()
            for row in await tools.users(unit=_live())
            if row["is_admin"] and not row.get("disabled_at")}


async def mind_owner(name: str) -> str | None:
    """The owner of the mind registered under `name`, "" for a mind with no
    owner, or None when `name` is no mind's."""
    # Matched as comms matches a mind's own credential to its row - case and
    # surrounding space aside - so a mind registered as "Arnold" or " arnold"
    # is still the mind its token was minted for.
    wanted = name.strip().lower()
    for row in await core.list_minds(unit=_live()):
        if str(row["name"] or "").strip().lower() == wanted:
            return str(row["owner"] or "").strip().lower()
    return None


# ------------------------------------------------------------------- tokens

async def create_token(name: str, token_hash: str, created_at: int) -> None:
    await tools.create_token(name, token_hash, created_at, unit=_live())


async def live_token(token_hash: str) -> Row | None:
    """The token a digest belongs to, while it is still valid."""
    return await tools.live_token(token_hash, unit=_live())


async def record_token_use(token_id: int, at: int) -> None:
    await tools.record_token_use(token_id, at, unit=_live())


async def tokens() -> list[Row]:
    """Every token, newest first, and never the digests."""
    return await tools.tokens(unit=_live())


async def register_token(name: str, token_hash: str, created_at: int) -> None:
    """Make a supplied digest the holder's one live credential.

    Raises `hive_data.Conflict` naming the holder when another holder has the
    digest, live or revoked."""
    await tools.register_token(name, token_hash, created_at, unit=_live())


async def holder_tokens() -> list[Row]:
    """One row per holder, never a digest."""
    return await tools.holder_tokens(unit=_live())


async def revoke_holder_tokens(name: str, at: int) -> int:
    return int((await tools.revoke_holder_tokens(name, at,
                                                 unit=_live()))["rows"])


async def remove_holder_tokens(name: str) -> int:
    return int((await tools.remove_holder_tokens(name, unit=_live()))["rows"])


async def expire_pending_requests(caller_mind: str, now: int) -> int:
    return int((await tools.expire_pending_requests(caller_mind, now,
                                                    unit=_live()))["rows"])


async def mind_names() -> set[str]:
    """Every registered mind's name, lowercased."""
    return {str(row["name"] or "").strip().lower()
            for row in await core.list_minds(unit=_live())}


async def revoke_token(token_id: int, at: int) -> int:
    """Revoke a token if it is not revoked already, reporting what changed."""
    return int((await tools.revoke_token(token_id, at,
                                         unit=_live()))["rows"])


# ----------------------------------------------------------------- settings

async def settings() -> list[Row]:
    return await tools.hitl_settings(unit=_live())


async def setting(tool_name: str) -> Row | None:
    return await tools.hitl_setting(tool_name, unit=_live())


async def set_setting(tool_name: str, mode: str,
                      sudo_timeout_minutes: int) -> None:
    """The operator's own edit: replace whatever is there."""
    await tools.set_hitl_setting(tool_name, mode, sudo_timeout_minutes,
                                 unit=_live())


async def seed_setting(tool_name: str, mode: str,
                       sudo_timeout_minutes: int) -> int:
    """Give a tool a mode only if it has none, reporting whether it was new.

    Insert-if-absent, deliberately. This runs on every boot for every gated
    tool, and an operator who turned a gate off expects it to stay off
    across a restart.
    """
    return int((await tools.seed_hitl_setting(
        tool_name, mode, sudo_timeout_minutes, unit=_live()))["rows"])


# ---------------------------------------------------------------- approvals

async def create_request(request_id: str, tool_name: str, caller_mind: str,
                         params_json: str, created_at: int, expires_at: int,
                         tool_method: str | None, tool_path: str | None,
                         tool_body: str | None) -> None:
    await tools.create_request(
        request_id, tool_name, caller_mind, params_json, created_at,
        expires_at, tool_method=tool_method, tool_path=tool_path,
        tool_body=tool_body, unit=_live())


async def request(request_id: str) -> Row | None:
    """One request, without the call it captured."""
    return await tools.request(request_id, unit=_live())


async def recent_requests(limit: int = QUEUE_LIMIT) -> list[Row]:
    """The queue the approvals page renders: newest first, capped.

    Without the captured bodies. On the live data the newest fifty rows
    carry megabytes of them, some holding credentials, and the page shows
    none of it.
    """
    return await tools.recent_requests(limit=limit, unit=_live())


async def decide_request(request_id: str, status: str, decided_at: int,
                         now: int) -> Row | None:
    """Decide a request still waiting at `now`, and hand back the call.

    The guard is in the statement. A request already decided, or past its
    expiry, changes nothing and answers None - so two people reaching the
    queue at the same moment produce one decision and the loser is told it
    lost.
    """
    written = await tools.decide_request(request_id, status, decided_at, now,
                                         unit=_live())
    return written["row"] if written["rows"] else None


async def record_outcome(request_id: str, ok: bool, status: int | None,
                         body: str, at: int) -> int:
    """Store what an approved request's replay did, once; report if it did."""
    return int((await tools.record_outcome(
        request_id, ok, status, body, at, unit=_live()))["rows"])


async def request_outcome(request_id: str) -> Row | None:
    """A request's replay outcome, with the body the tool answered."""
    return await tools.request_outcome(request_id, unit=_live())


# ------------------------------------------------------------------- grants

async def active_grant(tool_name: str, caller_mind: str,
                       now: int) -> Row | None:
    """A sudo grant still live at `now`, for this mind and this tool."""
    return await tools.active_grant(tool_name, caller_mind, now,
                                    unit=_live())


async def grant_sudo(tool_name: str, caller_mind: str, granted_at: int,
                     expires_at: int) -> None:
    await tools.grant_sudo(tool_name, caller_mind, granted_at, expires_at,
                           unit=_live())
