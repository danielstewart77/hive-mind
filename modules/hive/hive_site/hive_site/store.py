"""Where the hive's site data lives, as one async surface over the hive's Postgres.

Nothing in the console or the terminal opens a database or writes SQL. Every
page and every route calls a named operation here, and every operation is
answered by the hive's one Postgres through `hive_data`, at
`HIVE_DATABASE_URL`. There is no other store: the SQLite file each site kept
before the cutover is retired, and a site with no `HIVE_DATABASE_URL` refuses
to start rather than opening one.

The composite rules are not here. Which parents an item may be given, what a
board drop does to two sibling lists, when a design page may advance, whether
a change would leave the hive with no enabled admin — those live in
`kanban_data`, `design_data` and `auth`, once, on top of these operations.

Async, because `hive_data` is async only.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import time
from collections.abc import Iterator
from typing import Any, Literal

import hive_data
from hive_data import board, core, memory

#: The variable naming the database. Without it there is nothing to open.
DSN_VAR = "HIVE_DATABASE_URL"

#: The widest page the memory browser answers, whatever it was asked for.
#: Mirrors `hive_data.memory.BROWSE_LIMIT_MAX`.
MEMORY_LIMIT_MAX = 1000
MEMORY_LIMIT_DEFAULT = 200

Row = dict[str, Any]

#: Whether the store is open in this process.
_open = False

#: Whether this store was opened and then closed, as against never opened.
#: The two are different sentences: the first is a straggling background task
#: reaching a store the lifespan has already shut down, which is ordinary at
#: shutdown, and the second is a route running outside the app. Set only once
#: an open has actually succeeded, so a failed open does not report as a
#: shutdown — which is the one misdiagnosis `_unopened` exists to prevent.
_was_open = False

#: The unit of work the current task is inside, if any. A context variable
#: rather than a parameter on seventy operations, so a call site reads the
#: same whether or not it is in a transaction.
_unit: contextvars.ContextVar[Any] = contextvars.ContextVar("unit",
                                                            default=None)


# ------------------------------------------------------------------ lifecycle

def is_open() -> bool:
    """Whether a store is open in this process.

    For a test suite where a module-scoped live server owns the store and
    the per-test fixture must leave it alone.
    """
    return _open


async def open() -> None:
    """Open the hive's Postgres, or refuse when none is named.

    Called from the application's lifespan rather than at import, because
    opening a pool is async and because a module that connected on import
    would do it again in every test that imported it.
    """
    global _open, _was_open
    if not os.environ.get(DSN_VAR, "").strip():
        raise RuntimeError(
            f"{DSN_VAR} is unset. This site's data lives in the hive's "
            "Postgres and nowhere else — the SQLite file it once fell back "
            f"to is retired — so name the database in {DSN_VAR} and start "
            "it again.")
    await hive_data.connect()
    _open = True
    _was_open = True
    await _seed_admins_group()


#: The group admin *is*. Nothing else grants it, and the console's groups
#: page offers no way to create it.
ADMINS_GROUP = "admins"


async def _seed_admins_group() -> None:
    """Make sure the group admin is exists.

    Unconditional, on every open. Left to the first
    `create_user(is_admin=True)`, a store holding people but no admin yet has
    no such group at all — the groups page omits it, and adding the first
    admin to it answers "No such group", with nothing on screen saying the
    row is simply missing.
    """
    await create_group(name=ADMINS_GROUP, created_at=int(time.time()))


async def close() -> None:
    """Shut the store down. Nothing holds a connection afterwards.

    Marked closed first, so a straggling background call arriving after this
    reports a closed store.
    """
    global _open
    was, _open = _open, False
    if was:
        await hive_data.close(drain_timeout=5.0)


def _unopened() -> RuntimeError:
    """Why there is no store, said as the thing that actually happened.

    A store that was opened and closed is a straggling background task — a
    design notice being delivered, a credential probe — reaching a database
    after the lifespan shut it down, which is ordinary at shutdown. A store
    that was never opened is a route running outside the app. Folding the two
    together sends somebody hunting the second when it was the first.
    """
    if _was_open:
        return RuntimeError(
            "this site's store is closed. The lifespan shut it down, so "
            "this is work that outlived the application rather than a route "
            "reaching a database too early.")
    return RuntimeError(
        "this site's store was never opened. The application's lifespan "
        "opens it; a caller reaching a database before that is a route "
        "running outside the app.")


def _live() -> Any:
    """The `hive_data` unit this task is inside, or None; refused when closed."""
    if not _open:
        raise _unopened()
    return _unit.get()


# ---------------------------------------------------------------- units of work

def in_transaction() -> bool:
    """Whether this task is inside a unit of work."""
    return _unit.get() is not None


class transaction:
    """Several operations, all of them or none.

    Every board mutation needs one. Each reads the sibling list, decides a
    new numbering against it and writes the whole list back, so two of them
    interleaved is one tab's drag silently replaced by the other's, with both
    reporting success and two siblings left holding one position. That is
    what `hold_board` buys.

    A class rather than an `@asynccontextmanager`, and the reason is the
    access layer refusing the generator version outright. `hive_data` refuses
    to open a unit of work inside an async generator, because a generator
    yields to its caller while holding one pooled connection inside an open
    transaction, and a caller that abandons it leaves the transaction to
    unwind whenever the event loop's asyncgen finalizer reaches it. A
    generator wrapper around that unit is the same hazard with this module's
    name on it.

    The unit travels in a context variable, so the operations inside need no
    extra argument and every call site reads the same in or out of one.
    """

    def __init__(self, *, hold: str | None = None) -> None:
        self._scope: Any = None
        self._token: Any = None
        self._hold = hold

    async def __aenter__(self) -> None:
        if _unit.get() is not None:
            raise RuntimeError(
                "this site's store does not nest transactions. An inner "
                "block would end the outer one's work on its way out, so a "
                "failure after it would roll back nothing. Open one unit at "
                "the top of the operation.")
        if not _open:
            raise _unopened()
        self._scope = hive_data.transaction()
        unit = await self._scope.__aenter__()
        try:
            if self._hold is not None:
                # First statement in the unit, which `Unit.hold` enforces: a
                # hold taken after a read does not protect that read.
                await unit.hold(self._hold)
        except BaseException:
            # `__aenter__` raising means `async with` never calls
            # `__aexit__`, so the scope has to be ended here. Left abandoned
            # it is a suspended async generator holding an open transaction,
            # a pooled connection and the board's advisory lock until the
            # event loop's finalizer reaches it — the exact hazard
            # `hive_data` refuses a unit in a generator for, reintroduced
            # under this module's name.
            self._scope, scope = None, self._scope
            await scope.__aexit__(type(None), None, None)
            raise
        self._token = _unit.set(unit)
        return None

    async def __aexit__(self, kind: Any, value: Any,
                        trace: Any) -> Literal[False]:
        """End the unit, and never swallow what ended it.

        `Literal[False]` rather than `bool`: this construct does not suppress
        an exception, and saying so is what lets a caller `return` from inside
        the block. `hive_data`'s own unit re-raises every failure by design,
        so there is nothing here to pass on.
        """
        # The context variable is reset before the commit or the rollback, so
        # the statement that ends the unit is not itself run as part of one.
        _unit.reset(self._token)
        await self._scope.__aexit__(kind, value, trace)
        return False


def board_transaction() -> transaction:
    """A unit of work that serialises against every other board write."""
    return transaction(hold=board.BOARD_KEY)


# ---------------------------------------------------------------- work items

#: The lowest position a created row may take. Anything below it is a row
#: somebody placed deliberately, which is what `first_position` returns.
MIN_POSITION = 1


async def work_items(kind: str | None = None) -> list[Row]:
    return await board.work_items(kind=kind, unit=_live())


async def work_item(item_id: str) -> Row | None:
    return await board.work_item(item_id, unit=_live())


async def children(parent_id: str | None, kind: str | None = None) -> list[Row]:
    return await board.children(parent_id, kind=kind, unit=_live())


async def descendants(item_id: str) -> list[Row]:
    return await board.descendants(item_id, unit=_live())


async def next_position() -> int:
    """A position after every row the board has ever held.

    `MAX(position) + 1` over the whole table, not a count of what is there. A
    created row has to sort after everything that exists *and* after
    everything that has been deleted, or deleting from the middle hands the
    next row a slot it has to share.
    """
    return await board.next_position(unit=_live())


async def first_position() -> int:
    """A position before every row there is, so a new epic lands at the top."""
    return await board.first_position(unit=_live())


async def create_work_item(item_id: str, kind: str, title: str, position: int,
                           *, parent_id: str | None = None,
                           status: str | None = None, subtype: str = "",
                           description: str = "", requirements: str = "",
                           acceptance_criteria: str = "",
                           qa_tests: str = "") -> int:
    written = await board.create_work_item(
        item_id, kind, title, position, parent_id=parent_id,
        status=status, subtype=subtype, description=description,
        requirements=requirements,
        acceptance_criteria=acceptance_criteria, qa_tests=qa_tests,
        unit=_live())
    return int(written["rows"])


async def update_work_item(item_id: str, fields: dict[str, Any]) -> int:
    if not fields:
        return 0
    written = await board.update_work_item(item_id, fields, unit=_live())
    return int(written["rows"])


async def retype(item_id: str, kind: str, *, status: str | None = None,
                 subtype: str = "", parent_id: str | None = None,
                 position: int) -> int:
    """Change what an item *is*, in one statement.

    A retype is one fact made of five columns and every one follows from the
    new kind: an epic has no status and a story has one, a bug is a story
    carrying the bug subtype, a row becoming an epic loses its parent, and a
    row that moves parents needs a position because the number it held was
    only unique under the parent it left. Written one column at a time the
    board is briefly a feature with a story's status, or a row at a duplicate
    number, and the console reads it in between.
    """
    written = await board.retype(
        item_id, kind, status=status, subtype=subtype,
        parent_id=parent_id, position=position, unit=_live())
    return int(written["rows"])


async def place(item_id: str, parent_id: str | None, position: int) -> int:
    """Put a row under a parent at a position, in one statement.

    One statement for both, because they are one fact: a row whose parent
    moved and whose position did not is a row holding a number that was only
    unique under the parent it left.
    """
    written = await board.place(item_id, parent_id, position, unit=_live())
    return int(written["rows"])


async def renumber(ordered_ids: list[str], *, start: int = MIN_POSITION) -> int:
    """Give a sibling list consecutive positions."""
    if not ordered_ids:
        return 0
    written = await board.renumber(ordered_ids, start=start, unit=_live())
    return int(written["rows"])


async def delete_subtree(item_id: str) -> int:
    """Delete a row and everything beneath it, in one statement."""
    written = await board.delete_subtree(item_id, unit=_live())
    return int(written["rows"])


@contextlib.contextmanager
def _reference_refused() -> Iterator[None]:
    """Turn the access layer's missing-reference refusal into this module's.

    Scoped to the writes that carry a reference a caller supplied, rather
    than wrapped around everything: a `MissingReference` from a write whose
    references are all this module's own is a bug here, and dressing it as a
    bad input would hide it.
    """
    from hive_data.errors import MissingReference

    try:
        yield
    except MissingReference as exc:
        raise UnknownReference(str(exc)) from exc


# ------------------------------------------------------------ design sessions

async def create_design_session(design_id: str, title: str, created_at: float,
                                updated_at: float, *,
                                surface: str | None = None,
                                mind_id: str | None = None,
                                conversation_id: str | None = None,
                                repos: str = "[]") -> int:
    with _reference_refused():
        written = await board.create_design_session(
            design_id, title, created_at, updated_at, surface=surface,
            mind_id=mind_id, conversation_id=conversation_id,
            repos=repos, unit=_live())
    return int(written["rows"])


async def design_session(design_id: str) -> Row | None:
    return await board.design_session(design_id, unit=_live())


async def design_sessions() -> list[Row]:
    return await board.design_sessions(unit=_live())


async def attach_design_session(design_id: str,
                                conversation_id: str | None,
                                updated_at: float, *,
                                surface: str | None = None,
                                mind_id: str | None = None) -> int:
    """Point a design session at the conversation that is working on it.

    `surface` and `mind_id` are kept when not given: a mind re-claims a
    session on every turn that touches it and knows the conversation it is
    on, while the surface was decided when the page was opened. Writing None
    instead would blank the mind a page was published by on every attach.
    """
    with _reference_refused():
        written = await board.attach_design_session(
            design_id, conversation_id, updated_at, surface=surface,
            mind_id=mind_id, unit=_live())
    return int(written["rows"])


async def write_design_page(design_id: str, page: str, items: str,
                            accepted: str, stale: str, revision: int,
                            updated_at: float) -> int:
    """Write one page of a design session, if nobody else has since it was read.

    Guarded on the revision the caller read, so two tabs editing one page do
    not silently overwrite each other: a stale revision changes nothing and
    reports nothing changed.
    """
    written = await board.write_page(
        design_id, page, items, accepted, stale, revision, updated_at,
        unit=_live())
    return int(written["rows"])


async def delete_design_session(design_id: str) -> int:
    """Remove a design session and the notices that carry it back to a mind.

    One statement: `design_notices.design_id` carries no foreign key, so
    nothing in the database removes a session's notices when the session
    goes.
    """
    written = await board.delete_design_session(design_id, unit=_live())
    return int(written["rows"])


async def queue_notice(design_id: str, kind: str, page: str,
                       created_at: float, *, body: str = "") -> int | None:
    """Queue a notice telling a mind that a page was submitted.

    The id comes back because the database mints it, and the delivery path
    claims a notice by that id.
    """
    written = await board.queue_notice(design_id, kind, page, created_at,
                                       body=body, unit=_live())
    row = written.get("row")
    return None if row is None else int(row["id"])


async def pending_notices(design_id: str | None = None) -> list[Row]:
    return await board.pending_notices(design_id, unit=_live())


async def claim_notice(notice_id: int, claimed_at: float) -> Row | None:
    """Take a notice for delivery, once.

    Guarded on it being unclaimed in the statement, so the row *is* the
    arbitration: two deliverers reaching one notice means one of them gets
    None and stops rather than both sending it. Build is the one signal that
    starts a harness writing code, and delivered twice is two processes
    editing one working tree, each unaware of the other.
    """
    written = await board.claim_notice(notice_id, claimed_at, unit=_live())
    return written.get("row")


async def release_notice(notice_id: int) -> int:
    """Hand a claimed notice back when the send failed.

    Guarded on it not having been delivered, so an acknowledgement racing a
    failure report cannot un-deliver a notice that actually landed.
    """
    written = await board.release_notice(notice_id, unit=_live())
    return int(written["rows"])


async def mark_notice_delivered(notice_id: int, delivered_at: float) -> int:
    """Record that a notice was actually sent.

    Guarded on it not having been delivered already, so the row count is the
    answer: delivery is acknowledged after the fact, and a second
    acknowledgement for one send is a second send nobody made.
    """
    written = await board.mark_delivered(notice_id, delivered_at,
                                         unit=_live())
    return int(written["rows"])


async def notice(notice_id: int) -> Row | None:
    """One notice by id, whatever its state.

    The delivery path's own listing answers only undelivered notices, so a
    caller holding an id cannot tell "delivered" from "never existed".
    """
    return await board.notice(notice_id, unit=_live())


# ----------------------------------------------------- capability nodes, minds

async def set_capability_endpoint(node_id: str, endpoint: str,
                                  updated_at: int) -> int:
    written = await board.set_capability_endpoint(
        node_id, endpoint, updated_at, unit=_live())
    return int(written["rows"])


async def capability_endpoint(node_id: str) -> Row | None:
    return await board.capability_endpoint(node_id, unit=_live())


async def clear_capability_endpoint(node_id: str) -> int:
    written = await board.clear_capability_endpoint(node_id, unit=_live())
    return int(written["rows"])


async def set_edge_mind(name: str, profile: str, role: str, deployment: str,
                        updated_at: int) -> int:
    written = await board.set_edge_mind(name, profile, role, deployment,
                                        updated_at, unit=_live())
    return int(written["rows"])


async def edge_mind(name: str) -> Row | None:
    return await board.edge_mind(name, unit=_live())


async def edge_minds() -> list[Row]:
    return await board.edge_minds(unit=_live())


# ------------------------------------------------------ who started an operation

async def record_operation_requester(request_id: str, username: str,
                                     created_at: int) -> int:
    written = await board.record_operation_requester(
        request_id, username, created_at, unit=_live())
    return int(written["rows"])


async def operation_requester(request_id: str) -> str | None:
    """The person an operation belongs to, or None when nobody recorded one.

    None is what the route table turns into "only an admin may read this".
    """
    row = await board.operation_requester(request_id, unit=_live())
    return None if row is None else str(row["username"])


# ---------------------------------------------------------- people and groups

#: What a person's row carries. Named so the hash is not among them: it is
#: added only when a caller asks, which is sign-in and nothing else.
_USER_COLUMNS = ("id", "username", "created_at", "disabled_at",
                 "must_change", "email", "domain_account", "session_version")


async def get_user(username: str, *, private: bool = False) -> Row | None:
    return await core.get_user(username, private=private, unit=_live())


async def get_user_by_id(user_id: int, *, private: bool = False) -> Row | None:
    return await core.get_user_by_id(user_id, private=private, unit=_live())


async def list_users() -> list[Row]:
    return await core.list_users(unit=_live())


async def count_users() -> int:
    return await core.count_users(unit=_live())


async def find_user(*, email: str | None = None,
                    domain_account: str | None = None) -> Row | None:
    """The account an outside identity names, case-insensitively."""
    return await core.find_user(email=email, domain_account=domain_account,
                                unit=_live())


async def create_user(*, username: str, password_hash: str, created_at: int,
                      must_change: int = 0,
                      email: str | None = None,
                      domain_account: str | None = None) -> Row | None:
    """Write a person's account and hand back the row the database made.

    The id is the database's, so the row has to come back rather than be
    read afterwards by name — a read by name after the write is a second
    round trip that another writer can land between.
    """
    written = await core.create_user(
        username=username, password_hash=password_hash,
        created_at=created_at, must_change=must_change, email=email,
        domain_account=domain_account, unit=_live())
    row = written.get("row")
    # The access layer's insert returns every column, the hash included.
    # Withheld here so the rule the read side states — the hash travels only
    # when a caller asks for it — is true of the write side as well.
    return (None if row is None
            else {column: row[column] for column in _USER_COLUMNS})


#: What an update to a person may set. Declared rather than taken on trust,
#: because a column name cannot travel as a parameter and the alternative is
#: quoting whatever a caller passed.
USER_SETTABLE = frozenset({
    "password_hash", "disabled_at", "must_change", "email", "domain_account",
    "session_version",
})


async def update_user(user_id: int, **fields: Any) -> int:
    if not fields:
        return 0
    unknown = set(fields) - USER_SETTABLE
    if unknown:
        raise ValueError(
            f"a person's record has no settable column(s) {sorted(unknown)}")
    written = await core.update_user(user_id, unit=_live(), **fields)
    return int(written["rows"])


async def bump_session_version(user_id: int) -> int:
    """End every session this person holds, by moving the version they carry.

    An increment rather than a figure the caller computed: a caller that
    read the version, added one and wrote it back loses the other writer's
    bump, and the whole point of the column is ending a session that is open
    right now.
    """
    written = await core.bump_session_version(user_id, unit=_live())
    return int(written["rows"])


async def groups_of(user_id: int) -> list[str]:
    return await core.groups_of(user_id, unit=_live())


async def group_usernames(group_name: str) -> list[str]:
    return await core.group_usernames(group_name, unit=_live())


async def get_group(name: str) -> Row | None:
    return await core.get_group(name, unit=_live())


async def list_groups() -> list[Row]:
    return await core.list_groups(unit=_live())


async def create_group(*, name: str, created_at: int) -> int:
    """Create a group, or report that it was already there.

    The row count is the answer: nothing written means the group exists,
    which the console turns into a conflict rather than reporting a creation
    that did not happen.
    """
    written = await core.create_group(name=name, created_at=created_at,
                                      unit=_live())
    return int(written["rows"])


async def add_group_member(*, group_name: str, user_id: int) -> int:
    written = await core.add_group_member(group_name=group_name,
                                          user_id=user_id, unit=_live())
    return int(written["rows"])


async def remove_group_member(*, group_name: str, user_id: int) -> int:
    written = await core.remove_group_member(group_name=group_name,
                                             user_id=user_id, unit=_live())
    return int(written["rows"])


async def enabled_members_besides(*, group_name: str, user_id: int) -> int:
    """How many enabled people are in a group, not counting this one.

    The last-admin guard, as one statement inside the caller's own unit. Two
    admins disabling each other at the same moment both read a list that
    still holds the other, and both pass a check made against it.
    """
    return await core.enabled_members_besides(group_name=group_name,
                                              user_id=user_id, unit=_live())


# ----------------------------------------------------------------- auth settings

async def get_auth_setting(key: str) -> str | None:
    row = await core.get_auth_setting(key, unit=_live())
    return None if row is None else str(row["value"])


async def put_auth_setting_if_absent(key: str, value: str) -> str:
    """Store a setting unless it is already there, and answer with what holds.

    One statement rather than a read and a write. The cookie-signing key is
    generated on first use, and two workers reaching that at once with a read
    followed by a write issue two keys — which signs every cookie with
    whichever one landed second and signs the rest of the install out.
    """
    written = await core.put_auth_setting_if_absent(key, value, unit=_live())
    return str(written["row"]["value"])


# ---------------------------------------------------------------- sso bindings

async def get_sso_binding(*, provider: str, subject: str) -> Row | None:
    return await core.get_sso_binding(provider=provider, subject=subject,
                                      unit=_live())


async def put_sso_binding(*, provider: str, subject: str, username: str,
                          created_at: int) -> int:
    written = await core.put_sso_binding(
        provider=provider, subject=subject, username=username,
        created_at=created_at, unit=_live())
    return int(written["rows"])


async def sso_usernames(provider: str) -> set[str]:
    return set(await core.sso_usernames(provider, unit=_live()))


async def delete_sso_binding(*, provider: str, username: str) -> int:
    written = await core.delete_sso_binding(provider=provider,
                                            username=username, unit=_live())
    return int(written["rows"])


# -------------------------------------------------------------- memory browser

class UnknownReference(RuntimeError):
    """A write named a row another table does not hold.

    The one database has real foreign keys, so a reference that points
    nowhere is refused at the write. Raised as this module's own class rather
    than `hive_data`'s, so a caller catching it does not have to import the
    access layer.
    """


async def memory_tables() -> list[Row]:
    """The memory store's tables, each with how many rows it holds."""
    return await memory.tables(unit=_live())


async def memory_rows(table: str, *, limit: int = MEMORY_LIMIT_DEFAULT,
                      offset: int = 0) -> Row | None:
    """One page of a memory table, or None when there is no such table.

    None is what the console turns into a 404: a name that matches nothing is
    a typo in a URL, not a failure of the store.
    """
    return await memory.rows(table, limit=limit, offset=offset, unit=_live())
