"""Where the hive's site data lives, as one async surface with two backends.

Nothing in the console or the terminal opens a database or writes SQL. Every
page and every route calls a named operation here, and which store answers
is one value in the environment: `HIVE_DATABASE_URL` set means the hive's
Postgres through `hive_data`, unset means this host's own `hive.db` as
before. The two sites read that variable independently — one may be on
Postgres while the other is on the file.

Two backends rather than one, for the length of the migration only. The
SQLite file is left on disk and reachable, so a console that misbehaves on
Postgres is reverted by unsetting one variable and restarting rather than by
restoring a backup. Retiring the SQLite half is its own story and does not
happen until Postgres has run in anger.

The composite rules are not here. Which parents an item may be given, what a
board drop does to two sibling lists, when a design page may advance, whether
a change would leave the hive with no enabled admin — those live in
`kanban_data`, `design_data` and `auth`, once, on top of these operations, so
the two backends cannot disagree about them.

Async, because `hive_data` is async only and a synchronous face would mean
every query existing twice forever. The SQLite backend therefore runs its
statements off the event loop: the file is on local disk and the statements
are small, but blocking the loop for any of them would stall every other page
the process is serving.

**One connection per caller, each on the one thread that uses it.** A
`sqlite3.Connection` is not safe to use from two threads at once whatever
`check_same_thread` says — two statements interleaved on it return each
other's rows — so a connection here is created, used and closed inside a
single `asyncio.to_thread` call, and no two tasks ever hold the same one.

That is the shape the synchronous code had, and keeping it is not a detail.
A single shared connection with a transaction driven per request is a
connection inside *somebody else's* `BEGIN IMMEDIATE`: a second request's
ordinary write lands in that transaction and is discarded when the first
request rolls back — a design notice claimed, sent and acknowledged becomes
pending again because an unrelated board drag answered 404, and the next
sweep sends it a second time. Two harnesses in one working tree, which is
the one outcome the claim exists to prevent. A second `BEGIN IMMEDIATE` on
one connection is also an error rather than a wait, so two tabs dragging
cards would answer 500 where they used to take turns.

A unit of work therefore owns a connection of its own for its whole life, on
a thread of its own, and releases both when it ends. Two concurrent units
serialise the way they always did: through SQLite's own file-wide write lock
and `busy_timeout`, each waiting on its own thread rather than on the one
thread that has to do the committing.

The memory browser is the one operation whose two halves read two different
*databases*. On SQLite it reads lucent's own file, mounted read-only; on
Postgres it reads the `memory` schema of the one database. That is the window
this story closes — both stores are loaded and equal, so a console on
Postgres reading memory written to SQLite is the only way the two could
drift apart, and after the cutover there is one store.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, TypeVar

#: The variable that decides which store answers. Named rather than derived,
#: because the revert is an operator action and has to be one obvious edit.
DSN_VAR = "HIVE_DATABASE_URL"

#: The widest page the memory browser answers, whatever it was asked for.
#: Mirrors `hive_data.memory.BROWSE_LIMIT_MAX`; the two halves clamp the same.
MEMORY_LIMIT_MAX = 1000
MEMORY_LIMIT_DEFAULT = 200

#: A lucent table name has to look like one before it is quoted into a
#: statement. The Postgres half needs no such rule — it matches the name
#: against the database's own catalogue — but SQLite's `PRAGMA table_info`
#: takes an identifier rather than a parameter, so the SQLite half checks the
#: shape first and then checks the name against `sqlite_master` as well.
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

Row = dict[str, Any]

#: Which half is answering: "sqlite", "postgres", or None for a store that is
#: not open. One value rather than a flag and a connection, so a closed store
#: cannot read as the other engine — a `_postgres = False` on shutdown sent
#: every straggling call down the SQLite branch, where it raised only because
#: there happened to be nothing for it to use.
_engine: str | None = None

#: The file the SQLite half opens, fixed when the store opens rather than
#: read per connection: a test that moves `HIVE_DB_PATH` after opening would
#: otherwise have half its operations on one file and half on another.
_path: Path | None = None

#: Whether this store was opened and then closed, as against never opened.
#: The two are different sentences: the first is a straggling background task
#: reaching a store the lifespan has already shut down, which is ordinary at
#: shutdown, and the second is a route running outside the app. Set only once
#: an open has actually succeeded, so a failed open does not report as a
#: shutdown — which is the one misdiagnosis `_unopened` exists to prevent.
_was_open = False

#: The unit of work the current task is inside, if any. A context variable
#: rather than a parameter on seventy operations, so a call site reads the
#: same whether or not it is in a transaction — which is what keeps the two
#: halves from diverging at every caller.
_unit: contextvars.ContextVar[Any] = contextvars.ContextVar("unit",
                                                            default=None)

T = TypeVar("T")


# ------------------------------------------------------------------ lifecycle

def on_postgres() -> bool:
    """Which store is answering. For the status the health route reports."""
    return _engine == "postgres"


def is_open() -> bool:
    """Whether a store is open in this process.

    For a test suite where a module-scoped live server owns the store and
    the per-test fixture must leave it alone: opening a second one over the
    top points every later statement at a different file while the server
    is still serving the first.
    """
    return _engine is not None


def dsn_configured() -> bool:
    return bool(os.environ.get(DSN_VAR, "").strip())


#: Where the SQLite half's file is. Named, with no default.
PATH_VAR = "HIVE_DB_PATH"


def sqlite_path() -> Path:
    """The file the SQLite half keeps everything in, named explicitly.

    No default, and that is the point. This module used to live in the
    console's own `src/` and could fall back to the checkout's
    `data/hive.db` one directory up; installed into two images it cannot,
    and a guessed path here is the one failure this whole migration is
    about — a site that creates an empty store somewhere nobody mounted,
    answers every read with nothing, and reports itself healthy. So an
    unnamed file refuses to open rather than inventing one.
    """
    named = os.getenv(PATH_VAR, "").strip()
    if not named:
        raise RuntimeError(
            f"no {PATH_VAR} in the environment, and there is no default. "
            f"Either name the file this site's SQLite half should open, or "
            f"set {DSN_VAR} to put it on Postgres. Guessing a path here "
            "would create an empty database somewhere nothing is mounted "
            "and then serve from it.")
    path = Path(named)
    if not path.is_absolute():
        # Resolved against whatever directory the process happens to be in,
        # on every connection — so a site started from somewhere else opens
        # a fresh, empty, healthy-looking store. The same failure as an
        # unnamed file, arrived at by a relative one.
        raise RuntimeError(
            f"{PATH_VAR}={named!r} is relative. Name the file absolutely; a "
            "relative path opens a different database depending on the "
            "directory the process was started from.")
    return path


def lucent_path() -> str:
    """Where lucent's own file is mounted, for the SQLite half's browser."""
    return os.getenv("LUCENT_DB_PATH", "/data/lucent.db")


async def open(db_path: str | Path | None = None) -> None:
    """Open whichever store this process is pointed at.

    Called from the application's lifespan rather than at import, because
    opening a pool is async and because a module that connected on import
    would do it again in every test that imported it.

    On the SQLite half this creates the tables and brings an older file
    forward, once, on a connection of its own that is closed again — every
    operation after it opens its own.
    """
    global _engine, _path, _was_open
    if dsn_configured():
        import hive_data

        await hive_data.connect()
        _engine = "postgres"
        _path = None
        _was_open = True
        await _seed_admins_group()
        return
    _path = Path(db_path) if db_path is not None else sqlite_path()
    await asyncio.to_thread(_prepare_sqlite, _path)
    _engine = "sqlite"
    _was_open = True
    await _seed_admins_group()


#: The group admin *is*. Nothing else grants it, and the console's groups
#: page offers no way to create it.
ADMINS_GROUP = "admins"


async def _seed_admins_group() -> None:
    """Make sure the group admin is exists, on whichever store answers.

    Unconditional, as it was before this module: the console's own schema
    creation inserted it on every open. Left to the first
    `create_user(is_admin=True)`, a store holding people but no admin yet has
    no such group at all — the groups page omits it, and adding the first
    admin to it answers "No such group", with nothing on screen saying the
    row is simply missing.
    """
    await create_group(name=ADMINS_GROUP, created_at=int(time.time()))


async def close() -> None:
    """Shut the store down. Nothing holds a connection afterwards.

    The engine is cleared first, so a straggling background call arriving
    after this reports a closed store rather than being sent down the other
    half's branch.
    """
    global _engine, _path
    engine, _engine = _engine, None
    _path = None
    if engine == "postgres":
        import hive_data

        await hive_data.close(drain_timeout=5.0)


#: Columns `work_items` gained after the board shipped. An older file is
#: brought forward as the store opens rather than refusing to read: every
#: read names these columns, so a file without them answers "no such column"
#: on the board page rather than on anything that could say why.
_ADDED_ITEM_COLUMNS = {
    "description": "TEXT NOT NULL DEFAULT ''",
    "requirements": "TEXT NOT NULL DEFAULT ''",
    "acceptance_criteria": "TEXT NOT NULL DEFAULT ''",
    "qa_tests": "TEXT NOT NULL DEFAULT ''",
    "subtype": "TEXT NOT NULL DEFAULT ''",
}

_ADDED_USER_COLUMNS = {
    "must_change": "INTEGER NOT NULL DEFAULT 0",
    "email": "TEXT",
    "domain_account": "TEXT",
    "session_version": "INTEGER NOT NULL DEFAULT 0",
}

#: Every table the console's own file carries, created if it is not there.
#: One place rather than four modules each creating its own on connect: the
#: file is one file, and a module that created only its own tables left the
#: shape of the database depending on which page was loaded first.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    parent_id TEXT REFERENCES work_items(id),
    title TEXT NOT NULL,
    status TEXT,
    position INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    requirements TEXT NOT NULL DEFAULT '',
    acceptance_criteria TEXT NOT NULL DEFAULT '',
    qa_tests TEXT NOT NULL DEFAULT '',
    subtype TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS work_items_kind_position
    ON work_items (kind, position);
CREATE INDEX IF NOT EXISTS work_items_parent ON work_items (parent_id);
CREATE TABLE IF NOT EXISTS design_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    conversation_id TEXT,
    surface TEXT,
    mind_id TEXT,
    repos TEXT NOT NULL DEFAULT '[]',
    requirements TEXT NOT NULL DEFAULT '{}',
    tests TEXT NOT NULL DEFAULT '{}',
    functions TEXT NOT NULL DEFAULT '{}',
    accepted TEXT NOT NULL DEFAULT '{}',
    stale TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS design_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    page TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    claimed_at REAL,
    delivered_at REAL
);
CREATE TABLE IF NOT EXISTS capability_node_settings (
    node_id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS edge_mind_settings (
    name TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    role TEXT NOT NULL,
    deployment TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS operation_requesters (
    request_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    disabled_at INTEGER
);
CREATE TABLE IF NOT EXISTS groups (
    name TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    group_name TEXT NOT NULL REFERENCES groups(name),
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (group_name, user_id)
);
CREATE TABLE IF NOT EXISTS auth_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sso_bindings (
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    username TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (provider, subject),
    UNIQUE (provider, username)
);
"""


#: How long a statement waits for another writer's lock before giving up.
#: The same ten seconds every connection the console opened before this
#: module existed waited: in rollback-journal mode a writer locks the whole
#: file, so a board transaction allowed to hold longer than a login will wait
#: is a failed login write.
_BUSY_TIMEOUT_MS = 10_000


def _connect(path: Path) -> sqlite3.Connection:
    """One connection, for the one thread that is about to use it.

    `check_same_thread` is left at its default rather than turned off: a
    connection here never leaves the thread that made it, and saying so is
    what makes the rest of this half safe to reason about.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # `isolation_level=None` — this store drives its own transactions.
    # Python's sqlite3 otherwise opens one implicitly before a write and
    # closes it on `commit()`, which fights an explicit `BEGIN IMMEDIATE`.
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _prepare_sqlite(path: Path) -> None:
    """Create the console's own tables and bring an older file forward.

    Runs once, as the store opens, rather than on every connect — which is
    what four modules each doing their own `CREATE TABLE IF NOT EXISTS`
    amounted to.
    """
    connection = _connect(path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
        items = {row[1] for row in connection.execute(
            "PRAGMA table_info(work_items)")}
        for column, ddl in _ADDED_ITEM_COLUMNS.items():
            if column not in items:
                connection.execute(
                    f"ALTER TABLE work_items ADD COLUMN {column} {ddl}")
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(users)")}
        for column, ddl in _ADDED_USER_COLUMNS.items():
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE users ADD COLUMN {column} {ddl}")
        if "is_admin" in columns:
            # An older file held admin as a column. Every account it marked
            # joins the admins group and the column goes, in this same
            # transaction, so no reader ever sees an admin who is neither.
            connection.execute(
                "INSERT OR IGNORE INTO groups (name, created_at) "
                "SELECT 'admins', 0 WHERE NOT EXISTS "
                "(SELECT 1 FROM groups WHERE name = 'admins')")
            connection.execute(
                "INSERT OR IGNORE INTO group_members (group_name, user_id) "
                "SELECT 'admins', id FROM users WHERE is_admin = 1")
            connection.execute("ALTER TABLE users DROP COLUMN is_admin")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique "
            "ON users(lower(email)) WHERE email IS NOT NULL AND email != ''")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_domain_account_unique "
            "ON users(lower(domain_account)) "
            "WHERE domain_account IS NOT NULL AND domain_account != ''")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


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


async def _off_loop(run: Callable[[sqlite3.Connection], T]) -> T:
    """Run one piece of SQLite work, on a connection of its own.

    Inside a unit of work the unit's connection and the unit's thread are
    used, so the statement is part of that transaction. Outside one, a
    connection is opened, used and closed inside a single thread — nothing is
    shared, so no task can land a statement inside another task's
    transaction, and two writers take turns through SQLite's own file lock
    rather than through this process.
    """
    unit = _sqlite_unit()
    if unit is not None:
        return await unit.run(run)
    if _engine != "sqlite":
        raise _unopened()
    path = _path
    assert path is not None  # set with the engine, cleared with it

    def once() -> T:
        connection = _connect(path)
        try:
            return run(connection)
        finally:
            connection.close()

    return await asyncio.to_thread(once)


async def _one(sql: str, args: tuple[Any, ...] = ()) -> Row | None:
    def run(connection: sqlite3.Connection) -> Row | None:
        row = connection.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    return await _off_loop(run)


async def _all(sql: str, args: tuple[Any, ...] = ()) -> list[Row]:
    def run(connection: sqlite3.Connection) -> list[Row]:
        return [dict(r) for r in connection.execute(sql, args).fetchall()]

    return await _off_loop(run)


async def _write(sql: str, args: tuple[Any, ...] = ()) -> int:
    def run(connection: sqlite3.Connection) -> int:
        # No `commit()`. The connection is in autocommit mode, so a statement
        # outside a transaction has already landed, and one inside a unit of
        # work is the unit's to commit.
        return connection.execute(sql, args).rowcount

    return await _off_loop(run)


# ---------------------------------------------------------------- units of work

class _SqliteUnit:
    """A connection and the one thread that touches it, for one transaction.

    Its own connection, not a shared one: a transaction driven on a
    connection other tasks also write to puts their statements inside it, and
    they are discarded when it rolls back. Its own thread, because the
    waiting is the point — a second unit's `BEGIN IMMEDIATE` blocks until the
    first commits, and if both waited on one thread the waiter would be
    holding the thread the committer needs.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hive-console-unit")
        self._connection: sqlite3.Connection | None = None
        self._task: Any = None

    async def run(self, action: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run one statement on this unit's connection, on its own thread.

        Refused from any task but the one that opened it, which is what
        `hive_data.Unit` does on the other half. A task created inside a unit
        inherits it through the context variable, and a transaction is one
        connection: two tasks writing through it can each roll back the
        other's committed-looking work. Without this the same code would
        silently work here and raise there, which is the one thing the revert
        lever must not do.
        """
        running = asyncio.current_task()
        if self._task is not None and running is not self._task:
            raise RuntimeError(
                "a unit of work cannot be used from a second task. A "
                "transaction is one connection, so sharing it means each "
                "task can roll back the other's committed-looking work. Do "
                "the writes in the task that opened the unit, or open a "
                "unit of its own.")
        connection = self._connection
        assert connection is not None  # opened by `begin`, cleared by `end`
        return await asyncio.get_running_loop().run_in_executor(
            self._worker, lambda: action(connection))

    async def begin(self) -> None:
        """Take the file's write lock, or release everything and raise.

        The connection is opened on this unit's own thread and the `BEGIN`
        runs there too, so a cancellation that lands while the lock is being
        waited for cannot leave an open transaction behind on a connection
        anybody else will ever see: this one is closed on the way out and
        nothing else has a reference to it.
        """
        loop = asyncio.get_running_loop()

        def start() -> sqlite3.Connection:
            connection = _connect(self._path)
            try:
                connection.execute("BEGIN IMMEDIATE")
            except BaseException:
                connection.close()
                raise
            return connection

        try:
            self._connection = await loop.run_in_executor(self._worker, start)
        except BaseException:
            self._worker.shutdown(wait=False)
            raise
        self._task = asyncio.current_task()

    async def end(self, *, commit: bool) -> None:
        connection, self._connection = self._connection, None
        try:
            if connection is not None:
                await asyncio.get_running_loop().run_in_executor(
                    self._worker,
                    lambda: _finish(connection, commit=commit))
        finally:
            self._worker.shutdown(wait=False)


def _finish(connection: sqlite3.Connection, *, commit: bool) -> None:
    """Commit or roll back, and close either way.

    The close is in a `finally` because a failed `COMMIT` — the file's lock
    lost to another writer after a long transaction — would otherwise leak
    the connection and leave the journal behind.
    """
    try:
        connection.execute("COMMIT" if commit else "ROLLBACK")
    finally:
        connection.close()


def _sqlite_unit() -> _SqliteUnit | None:
    """The SQLite unit of work this task is inside, or None."""
    unit = _unit.get()
    return unit if isinstance(unit, _SqliteUnit) else None


def _pg_unit() -> Any:
    """The `hive_data` unit this task is inside, or None."""
    unit = _unit.get()
    return None if isinstance(unit, _SqliteUnit) else unit


def in_transaction() -> bool:
    """Whether this task is inside a unit of work."""
    return _unit.get() is not None


class transaction:
    """Several operations, all of them or none, on whichever store answers.

    Both halves, because a construct that only worked on one of them would be
    a caller that reads the same and behaves differently depending on an
    environment variable — which is the one thing the revert lever must not
    be.

    Every board mutation needs one. Each reads the sibling list, decides a
    new numbering against it and writes the whole list back, so two of them
    interleaved is one tab's drag silently replaced by the other's, with both
    reporting success and two siblings left holding one position. That is
    what `BEGIN IMMEDIATE` bought on SQLite and what `hold_board` buys here.

    A class rather than an `@asynccontextmanager`, and the reason is the
    access layer refusing the generator version outright. `hive_data` refuses
    to open a unit of work inside an async generator, because a generator
    yields to its caller while holding one pooled connection inside an open
    transaction, and a caller that abandons it leaves the transaction to
    unwind whenever the event loop's asyncgen finalizer reaches it. A
    generator wrapper around that unit is the same hazard with this module's
    name on it, and the guard could not tell the two apart because there is
    nothing to tell apart.

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
                "this site's store does not nest transactions. SQLite has "
                "no savepoint here and the inner block would commit the "
                "outer one's work on its way out, so a failure after it "
                "would roll back nothing. Open one unit at the top of the "
                "operation.")
        if _engine == "postgres":
            import hive_data

            self._scope = hive_data.transaction()
            unit = await self._scope.__aenter__()
            try:
                if self._hold is not None:
                    # First statement in the unit, which `Unit.hold`
                    # enforces: a hold taken after a read does not protect
                    # that read.
                    await unit.hold(self._hold)
            except BaseException:
                # `__aenter__` raising means `async with` never calls
                # `__aexit__`, so the scope has to be ended here. Left
                # abandoned it is a suspended async generator holding an open
                # transaction, a pooled connection and the board's advisory
                # lock until the event loop's finalizer reaches it — the
                # exact hazard `hive_data` refuses a unit in a generator for,
                # reintroduced under this module's name.
                self._scope, scope = None, self._scope
                await scope.__aexit__(type(None), None, None)
                raise
            self._token = _unit.set(unit)
            return None
        if _engine != "sqlite":
            raise _unopened()
        assert _path is not None  # set with the engine, cleared with it
        unit = _SqliteUnit(_path)
        await unit.begin()
        self._token = _unit.set(unit)
        return None

    async def __aexit__(self, kind: Any, value: Any,
                        trace: Any) -> Literal[False]:
        """End the unit, and never swallow what ended it.

        `Literal[False]` rather than `bool`: this construct does not suppress
        an exception, and saying so is what lets a caller `return` from inside
        the block. A unit that suppressed the failure it rolled back for would
        hand its caller a successful-looking return from a block whose writes
        are gone — and `hive_data`'s own unit re-raises every failure by
        design, so there is nothing here to pass on.
        """
        # The context variable is reset before the commit or the rollback, so
        # the statement that ends the unit is not itself run as part of one.
        unit = _unit.get()
        _unit.reset(self._token)
        if self._scope is not None:
            await self._scope.__aexit__(kind, value, trace)
            return False
        assert isinstance(unit, _SqliteUnit)
        await unit.end(commit=kind is None)
        return False


def board_transaction() -> transaction:
    """A unit of work that serialises against every other board write."""
    if _engine == "postgres":
        from hive_data import board

        return transaction(hold=board.BOARD_KEY)
    return transaction()


# ---------------------------------------------------------------- work items

#: Every column of a work item plus the kind of the row above it. The
#: parent's kind decides whether a story's parent is reported as its feature
#: or as its epic, and reading it in the same statement is what keeps a
#: listing from being one query per row.
_ITEM_SELECT = """
    SELECT item.id, item.kind, item.parent_id, item.title, item.status,
           item.subtype, item.description, item.requirements,
           item.acceptance_criteria, item.qa_tests, item.position,
           parent.kind AS parent_kind
      FROM work_items AS item
      LEFT JOIN work_items AS parent ON parent.id = item.parent_id
"""

#: The board's own order. `rowid` breaks the tie on SQLite because SQL leaves
#: the order of equal keys undefined; Postgres has no `rowid` and breaks it
#: on `id`, which decides nothing the old order decided — `position` is
#: unique within a parent, so the only ties are between rows under different
#: parents in a flat listing every consumer regroups by parent.
_ITEM_ORDER = " ORDER BY item.position, item.rowid"

#: The lowest position a created row may take. Anything below it is a row
#: somebody placed deliberately, which is what `first_position` returns.
MIN_POSITION = 1


async def work_items(kind: str | None = None) -> list[Row]:
    if _engine == "postgres":
        from hive_data import board

        return await board.work_items(kind=kind, unit=_pg_unit())
    if kind is None:
        return await _all(_ITEM_SELECT + _ITEM_ORDER)
    return await _all(_ITEM_SELECT + " WHERE item.kind = ?" + _ITEM_ORDER,
                      (kind,))


async def work_item(item_id: str) -> Row | None:
    if _engine == "postgres":
        from hive_data import board

        return await board.work_item(item_id, unit=_pg_unit())
    return await _one(_ITEM_SELECT + " WHERE item.id = ?", (item_id,))


async def children(parent_id: str | None, kind: str | None = None) -> list[Row]:
    if _engine == "postgres":
        from hive_data import board

        return await board.children(parent_id, kind=kind, unit=_pg_unit())
    where = (" WHERE item.parent_id IS NULL" if parent_id is None
             else " WHERE item.parent_id = ?")
    args: list[Any] = [] if parent_id is None else [parent_id]
    if kind is not None:
        where += " AND item.kind = ?"
        args.append(kind)
    return await _all(_ITEM_SELECT + where + _ITEM_ORDER, tuple(args))


async def descendants(item_id: str) -> list[Row]:
    if _engine == "postgres":
        from hive_data import board

        return await board.descendants(item_id, unit=_pg_unit())
    return await _all(
        "WITH RECURSIVE below AS ("
        "  SELECT id FROM work_items WHERE parent_id = ?"
        "  UNION"
        "  SELECT child.id FROM work_items AS child"
        "    JOIN below ON below.id = child.parent_id"
        ") " + _ITEM_SELECT
        + " WHERE item.id IN (SELECT id FROM below)" + _ITEM_ORDER,
        (item_id,))


async def next_position() -> int:
    """A position after every row the board has ever held.

    `MAX(position) + 1` over the whole table, not a count of what is there. A
    created row has to sort after everything that exists *and* after
    everything that has been deleted, or deleting from the middle hands the
    next row a slot it has to share.
    """
    if _engine == "postgres":
        from hive_data import board

        return await board.next_position(unit=_pg_unit())
    row = await _one(
        "SELECT COALESCE(MAX(position), ?) + 1 AS next FROM work_items",
        (MIN_POSITION - 1,))
    assert row is not None  # an aggregate always answers
    return int(row["next"])


async def first_position() -> int:
    """A position before every row there is, so a new epic lands at the top."""
    if _engine == "postgres":
        from hive_data import board

        return await board.first_position(unit=_pg_unit())
    row = await _one(
        "SELECT COALESCE(MIN(position), ?) - 1 AS first FROM work_items",
        (MIN_POSITION,))
    assert row is not None
    return int(row["first"])


async def create_work_item(item_id: str, kind: str, title: str, position: int,
                           *, parent_id: str | None = None,
                           status: str | None = None, subtype: str = "",
                           description: str = "", requirements: str = "",
                           acceptance_criteria: str = "",
                           qa_tests: str = "") -> int:
    if _engine == "postgres":
        from hive_data import board

        written = await board.create_work_item(
            item_id, kind, title, position, parent_id=parent_id,
            status=status, subtype=subtype, description=description,
            requirements=requirements,
            acceptance_criteria=acceptance_criteria, qa_tests=qa_tests,
            unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "INSERT INTO work_items (id, kind, parent_id, title, status, subtype,"
        " description, requirements, acceptance_criteria, qa_tests, position)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, kind, parent_id, title, status, subtype, description,
         requirements, acceptance_criteria, qa_tests, position))


async def update_work_item(item_id: str, fields: dict[str, Any]) -> int:
    if not fields:
        return 0
    if _engine == "postgres":
        from hive_data import board

        written = await board.update_work_item(item_id, fields,
                                               unit=_pg_unit())
        return int(written["rows"])
    clause = ", ".join(f"{column} = ?" for column in fields)
    return await _write(
        f"UPDATE work_items SET {clause} WHERE id = ?",
        (*fields.values(), item_id))


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
    if _engine == "postgres":
        from hive_data import board

        written = await board.retype(
            item_id, kind, status=status, subtype=subtype,
            parent_id=parent_id, position=position, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "UPDATE work_items SET kind = ?, status = ?, subtype = ?,"
        " parent_id = ?, position = ? WHERE id = ?",
        (kind, status, subtype, parent_id, position, item_id))


async def place(item_id: str, parent_id: str | None, position: int) -> int:
    """Put a row under a parent at a position, in one statement.

    One statement for both, because they are one fact: a row whose parent
    moved and whose position did not is a row holding a number that was only
    unique under the parent it left.
    """
    if _engine == "postgres":
        from hive_data import board

        written = await board.place(item_id, parent_id, position,
                                    unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "UPDATE work_items SET parent_id = ?, position = ? WHERE id = ?",
        (parent_id, position, item_id))


async def renumber(ordered_ids: list[str], *, start: int = MIN_POSITION) -> int:
    """Give a sibling list consecutive positions."""
    if not ordered_ids:
        return 0
    if _engine == "postgres":
        from hive_data import board

        written = await board.renumber(ordered_ids, start=start,
                                       unit=_pg_unit())
        return int(written["rows"])

    def run(connection: sqlite3.Connection) -> int:
        changed = 0
        for position, item_id in enumerate(ordered_ids, start=start):
            changed += connection.execute(
                "UPDATE work_items SET position = ? WHERE id = ?",
                (position, item_id)).rowcount
        return changed

    return await _off_loop(run)


async def delete_subtree(item_id: str) -> int:
    """Delete a row and everything beneath it.

    One statement on both halves. SQLite's `foreign_keys` pragma is off by
    default and per connection, so a declared cascade was decorative on any
    connection that forgot it — and the orphans it left still rendered on the
    board while being unreachable from the only delete button. Postgres has a
    real key, deferred, checked once at commit with the whole subtree gone.
    """
    if _engine == "postgres":
        from hive_data import board

        written = await board.delete_subtree(item_id, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "WITH RECURSIVE below AS ("
        "  SELECT id FROM work_items WHERE id = ?"
        "  UNION"
        "  SELECT child.id FROM work_items AS child"
        "    JOIN below ON below.id = child.parent_id"
        ") DELETE FROM work_items WHERE id IN (SELECT id FROM below)",
        (item_id,))


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
    if _engine == "postgres":
        from hive_data import board

        with _reference_refused():
            written = await board.create_design_session(
                design_id, title, created_at, updated_at, surface=surface,
                mind_id=mind_id, conversation_id=conversation_id,
                repos=repos, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "INSERT INTO design_sessions (id, title, conversation_id, surface,"
        " mind_id, repos, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (design_id, title, conversation_id, surface, mind_id, repos,
         created_at, updated_at))


async def design_session(design_id: str) -> Row | None:
    if _engine == "postgres":
        from hive_data import board

        return await board.design_session(design_id, unit=_pg_unit())
    return await _one("SELECT * FROM design_sessions WHERE id = ?",
                      (design_id,))


async def design_sessions() -> list[Row]:
    if _engine == "postgres":
        from hive_data import board

        return await board.design_sessions(unit=_pg_unit())
    return await _all(
        "SELECT * FROM design_sessions ORDER BY updated_at DESC")


async def attach_design_session(design_id: str,
                                conversation_id: str | None,
                                updated_at: float, *,
                                surface: str | None = None,
                                mind_id: str | None = None) -> int:
    """Point a design session at the conversation that is working on it.

    `surface` and `mind_id` are kept when not given, which is what the
    `COALESCE` is for rather than a convenience: a mind re-claims a session
    on every turn that touches it and knows the conversation it is on, while
    the surface was decided when the page was opened. Writing None instead
    would blank the mind a page was published by on every attach.
    """
    if _engine == "postgres":
        from hive_data import board

        with _reference_refused():
            written = await board.attach_design_session(
                design_id, conversation_id, updated_at, surface=surface,
                mind_id=mind_id, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "UPDATE design_sessions SET conversation_id = ?,"
        " surface = COALESCE(?, surface), mind_id = COALESCE(?, mind_id),"
        " updated_at = ? WHERE id = ?",
        (conversation_id, surface, mind_id, updated_at, design_id))


async def write_design_page(design_id: str, page: str, items: str,
                            accepted: str, stale: str, revision: int,
                            updated_at: float) -> int:
    """Write one page of a design session, if nobody else has since it was read.

    Guarded on the revision the caller read, so two tabs editing one page do
    not silently overwrite each other: a stale revision changes nothing and
    reports nothing changed.
    """
    if _engine == "postgres":
        from hive_data import board

        written = await board.write_page(
            design_id, page, items, accepted, stale, revision, updated_at,
            unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        f"UPDATE design_sessions SET {page} = ?, accepted = ?, stale = ?,"
        " revision = revision + 1, updated_at = ?"
        " WHERE id = ? AND revision = ?",
        (items, accepted, stale, updated_at, design_id, revision))


async def delete_design_session(design_id: str) -> int:
    """Remove a design session and the notices that carry it back to a mind.

    One statement on Postgres: `design_notices.design_id` carries no foreign
    key, so nothing in the database removes a session's notices when the
    session goes. The console's own delete did it in two, on one connection.
    """
    if _engine == "postgres":
        from hive_data import board

        written = await board.delete_design_session(design_id,
                                                    unit=_pg_unit())
        return int(written["rows"])

    def run(connection: sqlite3.Connection) -> int:
        removed = connection.execute(
            "DELETE FROM design_sessions WHERE id = ?", (design_id,)).rowcount
        connection.execute("DELETE FROM design_notices WHERE design_id = ?",
                           (design_id,))
        return removed

    return await _off_loop(run)


async def queue_notice(design_id: str, kind: str, page: str,
                       created_at: float, *, body: str = "") -> int | None:
    """Queue a notice telling a mind that a page was submitted.

    The id comes back because the database mints it on both halves, and the
    delivery path claims a notice by that id.
    """
    if _engine == "postgres":
        from hive_data import board

        written = await board.queue_notice(design_id, kind, page, created_at,
                                           body=body, unit=_pg_unit())
        row = written.get("row")
        return None if row is None else int(row["id"])

    def run(connection: sqlite3.Connection) -> int | None:
        cursor = connection.execute(
            "INSERT INTO design_notices (design_id, kind, page, body,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (design_id, kind, page, body, created_at))
        return None if cursor.lastrowid is None else int(cursor.lastrowid)

    return await _off_loop(run)


#: What an undelivered notice is reported with. The notice's own columns and
#: the four the delivery path needs to send anything at all — joined rather
#: than read in a second statement, because a notice whose session was
#: deleted between the two reads is a send with nowhere to go.
_NOTICE_SELECT = """
    SELECT notice.id, notice.design_id, notice.kind, notice.page,
           notice.body, notice.created_at, notice.claimed_at,
           notice.delivered_at, session.title, session.conversation_id,
           session.surface, session.mind_id
      FROM design_notices AS notice
      JOIN design_sessions AS session ON session.id = notice.design_id
     WHERE notice.delivered_at IS NULL
"""


async def pending_notices(design_id: str | None = None) -> list[Row]:
    if _engine == "postgres":
        from hive_data import board

        return await board.pending_notices(design_id, unit=_pg_unit())
    order = " ORDER BY notice.created_at, notice.id"
    if design_id is None:
        return await _all(_NOTICE_SELECT + order)
    return await _all(
        _NOTICE_SELECT + " AND notice.design_id = ?" + order, (design_id,))


async def claim_notice(notice_id: int, claimed_at: float) -> Row | None:
    """Take a notice for delivery, once.

    Guarded on it being unclaimed in the statement, so the row *is* the
    arbitration: two deliverers reaching one notice means one of them gets
    None and stops rather than both sending it. Build is the one signal that
    starts a harness writing code, and delivered twice is two processes
    editing one working tree, each unaware of the other.
    """
    if _engine == "postgres":
        from hive_data import board

        written = await board.claim_notice(notice_id, claimed_at,
                                           unit=_pg_unit())
        return written.get("row")

    def run(connection: sqlite3.Connection) -> Row | None:
        taken = connection.execute(
            "UPDATE design_notices SET claimed_at = ?"
            " WHERE id = ? AND claimed_at IS NULL AND delivered_at IS NULL",
            (claimed_at, notice_id)).rowcount
        if taken == 0:
            return None
        row = connection.execute(
            "SELECT id, design_id, kind, page, body, created_at, claimed_at,"
            " delivered_at FROM design_notices WHERE id = ?",
            (notice_id,)).fetchone()
        return dict(row) if row is not None else None

    return await _off_loop(run)


async def release_notice(notice_id: int) -> int:
    """Hand a claimed notice back when the send failed.

    Guarded on it not having been delivered, so an acknowledgement racing a
    failure report cannot un-deliver a notice that actually landed.
    """
    if _engine == "postgres":
        from hive_data import board

        written = await board.release_notice(notice_id, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "UPDATE design_notices SET claimed_at = NULL"
        " WHERE id = ? AND delivered_at IS NULL",
        (notice_id,))


async def mark_notice_delivered(notice_id: int, delivered_at: float) -> int:
    """Record that a notice was actually sent.

    Guarded on it not having been delivered already, so the row count is the
    answer: delivery is acknowledged after the fact, and a second
    acknowledgement for one send is a second send nobody made.
    """
    if _engine == "postgres":
        from hive_data import board

        written = await board.mark_delivered(notice_id, delivered_at,
                                             unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "UPDATE design_notices SET delivered_at = ?"
        " WHERE id = ? AND delivered_at IS NULL",
        (delivered_at, notice_id))


async def notice(notice_id: int) -> Row | None:
    """One notice by id, whatever its state.

    The delivery path's own listing answers only undelivered notices, so a
    caller holding an id cannot tell "delivered" from "never existed".
    """
    if _engine == "postgres":
        from hive_data import board

        return await board.notice(notice_id, unit=_pg_unit())
    return await _one(
        "SELECT id, design_id, kind, page, body, created_at, claimed_at,"
        " delivered_at FROM design_notices WHERE id = ?", (notice_id,))


# ----------------------------------------------------- capability nodes, minds

async def set_capability_endpoint(node_id: str, endpoint: str,
                                  updated_at: int) -> int:
    if _engine == "postgres":
        from hive_data import board

        written = await board.set_capability_endpoint(
            node_id, endpoint, updated_at, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "INSERT INTO capability_node_settings (node_id, endpoint, updated_at)"
        " VALUES (?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET"
        " endpoint = excluded.endpoint, updated_at = excluded.updated_at",
        (node_id, endpoint, updated_at))


async def capability_endpoint(node_id: str) -> Row | None:
    if _engine == "postgres":
        from hive_data import board

        return await board.capability_endpoint(node_id, unit=_pg_unit())
    return await _one(
        "SELECT * FROM capability_node_settings WHERE node_id = ?", (node_id,))


async def clear_capability_endpoint(node_id: str) -> int:
    if _engine == "postgres":
        from hive_data import board

        written = await board.clear_capability_endpoint(node_id,
                                                        unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "DELETE FROM capability_node_settings WHERE node_id = ?", (node_id,))


async def set_edge_mind(name: str, profile: str, role: str, deployment: str,
                        updated_at: int) -> int:
    if _engine == "postgres":
        from hive_data import board

        written = await board.set_edge_mind(name, profile, role, deployment,
                                            updated_at, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "INSERT INTO edge_mind_settings (name, profile, role, deployment,"
        " updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET"
        " profile = excluded.profile, role = excluded.role,"
        " deployment = excluded.deployment, updated_at = excluded.updated_at",
        (name, profile, role, deployment, updated_at))


async def edge_mind(name: str) -> Row | None:
    if _engine == "postgres":
        from hive_data import board

        return await board.edge_mind(name, unit=_pg_unit())
    return await _one("SELECT * FROM edge_mind_settings WHERE name = ?",
                      (name,))


async def edge_minds() -> list[Row]:
    if _engine == "postgres":
        from hive_data import board

        return await board.edge_minds(unit=_pg_unit())
    return await _all("SELECT * FROM edge_mind_settings ORDER BY name")


# ------------------------------------------------------ who started an operation

async def record_operation_requester(request_id: str, username: str,
                                     created_at: int) -> int:
    if _engine == "postgres":
        from hive_data import board

        written = await board.record_operation_requester(
            request_id, username, created_at, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "INSERT INTO operation_requesters (request_id, username, created_at)"
        " VALUES (?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET"
        " username = excluded.username, created_at = excluded.created_at",
        (request_id, username, created_at))


async def operation_requester(request_id: str) -> str | None:
    """The person an operation belongs to, or None when nobody recorded one.

    None is what the route table turns into "only an admin may read this".
    """
    if _engine == "postgres":
        from hive_data import board

        row = await board.operation_requester(request_id, unit=_pg_unit())
    else:
        row = await _one(
            "SELECT username FROM operation_requesters WHERE request_id = ?",
            (request_id,))
    return None if row is None else str(row["username"])


# ---------------------------------------------------------- people and groups

#: What a person's row carries. Named rather than `SELECT *` so the two
#: halves hand back the same keys, and so the hash is not among them: it is
#: added only when a caller asks, which is sign-in and nothing else.
_USER_COLUMNS = ("id", "username", "created_at", "disabled_at",
                 "must_change", "email", "domain_account", "session_version")


async def get_user(username: str, *, private: bool = False) -> Row | None:
    if _engine == "postgres":
        from hive_data import core

        return await core.get_user(username, private=private,
                                   unit=_pg_unit())
    columns = ", ".join(
        _USER_COLUMNS + (("password_hash",) if private else ()))
    return await _one(f"SELECT {columns} FROM users WHERE username = ?",
                      (username,))


async def get_user_by_id(user_id: int, *, private: bool = False) -> Row | None:
    if _engine == "postgres":
        from hive_data import core

        return await core.get_user_by_id(user_id, private=private,
                                         unit=_pg_unit())
    columns = ", ".join(
        _USER_COLUMNS + (("password_hash",) if private else ()))
    return await _one(f"SELECT {columns} FROM users WHERE id = ?", (user_id,))


async def list_users() -> list[Row]:
    if _engine == "postgres":
        from hive_data import core

        return await core.list_users(unit=_pg_unit())
    return await _all(
        f"SELECT {', '.join(_USER_COLUMNS)} FROM users ORDER BY username")


async def count_users() -> int:
    if _engine == "postgres":
        from hive_data import core

        return await core.count_users(unit=_pg_unit())
    row = await _one("SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row else 0


async def find_user(*, email: str | None = None,
                    domain_account: str | None = None) -> Row | None:
    """The account an outside identity names, case-insensitively."""
    if _engine == "postgres":
        from hive_data import core

        return await core.find_user(email=email,
                                    domain_account=domain_account,
                                    unit=_pg_unit())
    if (email is None) == (domain_account is None):
        raise ValueError(
            "find_user takes exactly one of email or domain_account")
    column = "email" if email is not None else "domain_account"
    value = ((email if email is not None else domain_account) or "").strip()
    if not value:
        return None
    return await _one(
        f"SELECT {', '.join(_USER_COLUMNS)} FROM users "
        f"WHERE lower({column}) = lower(?)", (value,))


async def create_user(*, username: str, password_hash: str, created_at: int,
                      must_change: int = 0,
                      email: str | None = None,
                      domain_account: str | None = None) -> Row | None:
    """Write a person's account and hand back the row the database made.

    The id is the database's on both halves, so the row has to come back
    rather than be read afterwards by name — a read by name after the write
    is a second round trip that another writer can land between.
    """
    if _engine == "postgres":
        from hive_data import core

        written = await core.create_user(
            username=username, password_hash=password_hash,
            created_at=created_at, must_change=must_change, email=email,
            domain_account=domain_account, unit=_pg_unit())
        row = written.get("row")
        # The access layer's insert returns every column, the hash included.
        # Withheld here so the two halves hand back the same keys and so the
        # rule the read side states — the hash travels only when a caller
        # asks for it — is true of the write side as well.
        return (None if row is None
                else {column: row[column] for column in _USER_COLUMNS})

    def run(connection: sqlite3.Connection) -> Row | None:
        cursor = connection.execute(
            "INSERT INTO users (username, password_hash, created_at,"
            " must_change, email, domain_account) VALUES (?, ?, ?, ?, ?, ?)",
            (username, password_hash, created_at, must_change, email,
             domain_account))
        row = connection.execute(
            f"SELECT {', '.join(_USER_COLUMNS)} FROM users WHERE id = ?",
            (cursor.lastrowid,)).fetchone()
        return dict(row) if row is not None else None

    return await _off_loop(run)


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
    if _engine == "postgres":
        from hive_data import core

        written = await core.update_user(user_id, unit=_pg_unit(), **fields)
        return int(written["rows"])
    clause = ", ".join(f"{column} = ?" for column in fields)
    return await _write(f"UPDATE users SET {clause} WHERE id = ?",
                        (*fields.values(), user_id))


async def bump_session_version(user_id: int) -> int:
    """End every session this person holds, by moving the version they carry.

    An increment rather than a figure the caller computed: a caller that
    read the version, added one and wrote it back loses the other writer's
    bump, and the whole point of the column is ending a session that is open
    right now.
    """
    if _engine == "postgres":
        from hive_data import core

        written = await core.bump_session_version(user_id, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "UPDATE users SET session_version = session_version + 1 WHERE id = ?",
        (user_id,))


async def groups_of(user_id: int) -> list[str]:
    if _engine == "postgres":
        from hive_data import core

        return await core.groups_of(user_id, unit=_pg_unit())
    rows = await _all(
        "SELECT group_name FROM group_members WHERE user_id = ?"
        " ORDER BY group_name", (user_id,))
    return [row["group_name"] for row in rows]


async def group_usernames(group_name: str) -> list[str]:
    if _engine == "postgres":
        from hive_data import core

        return await core.group_usernames(group_name, unit=_pg_unit())
    rows = await _all(
        "SELECT u.username AS username FROM group_members AS m"
        " JOIN users AS u ON u.id = m.user_id"
        " WHERE m.group_name = ? ORDER BY u.username", (group_name,))
    return [row["username"] for row in rows]


async def get_group(name: str) -> Row | None:
    if _engine == "postgres":
        from hive_data import core

        return await core.get_group(name, unit=_pg_unit())
    return await _one("SELECT name, created_at FROM groups WHERE name = ?",
                      (name,))


async def list_groups() -> list[Row]:
    if _engine == "postgres":
        from hive_data import core

        return await core.list_groups(unit=_pg_unit())
    return await _all("SELECT name, created_at FROM groups ORDER BY name")


async def create_group(*, name: str, created_at: int) -> int:
    """Create a group, or report that it was already there.

    The row count is the answer: nothing written means the group exists,
    which the console turns into a conflict rather than reporting a creation
    that did not happen.
    """
    if _engine == "postgres":
        from hive_data import core

        written = await core.create_group(name=name, created_at=created_at,
                                          unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "INSERT INTO groups (name, created_at) VALUES (?, ?)"
        " ON CONFLICT(name) DO NOTHING", (name, created_at))


async def add_group_member(*, group_name: str, user_id: int) -> int:
    if _engine == "postgres":
        from hive_data import core

        written = await core.add_group_member(group_name=group_name,
                                              user_id=user_id,
                                              unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "INSERT INTO group_members (group_name, user_id) VALUES (?, ?)"
        " ON CONFLICT(group_name, user_id) DO NOTHING",
        (group_name, user_id))


async def remove_group_member(*, group_name: str, user_id: int) -> int:
    if _engine == "postgres":
        from hive_data import core

        written = await core.remove_group_member(group_name=group_name,
                                                 user_id=user_id,
                                                 unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "DELETE FROM group_members WHERE group_name = ? AND user_id = ?",
        (group_name, user_id))


async def enabled_members_besides(*, group_name: str, user_id: int) -> int:
    """How many enabled people are in a group, not counting this one.

    The last-admin guard, as one statement inside the caller's own unit. Two
    admins disabling each other at the same moment both read a list that
    still holds the other, and both pass a check made against it.
    """
    if _engine == "postgres":
        from hive_data import core

        return await core.enabled_members_besides(group_name=group_name,
                                                  user_id=user_id,
                                                  unit=_pg_unit())
    row = await _one(
        "SELECT COUNT(*) AS n FROM users AS u"
        " JOIN group_members AS m ON m.user_id = u.id"
        " WHERE m.group_name = ? AND u.disabled_at IS NULL AND u.id != ?",
        (group_name, user_id))
    return int(row["n"]) if row else 0


# ----------------------------------------------------------------- auth settings

async def get_auth_setting(key: str) -> str | None:
    if _engine == "postgres":
        from hive_data import core

        row = await core.get_auth_setting(key, unit=_pg_unit())
    else:
        row = await _one("SELECT value FROM auth_settings WHERE key = ?",
                         (key,))
    return None if row is None else str(row["value"])


async def put_auth_setting_if_absent(key: str, value: str) -> str:
    """Store a setting unless it is already there, and answer with what holds.

    One statement rather than a read and a write. The cookie-signing key is
    generated on first use, and two workers reaching that at once with a read
    followed by a write issue two keys — which signs every cookie with
    whichever one landed second and signs the rest of the install out.
    """
    if _engine == "postgres":
        from hive_data import core

        written = await core.put_auth_setting_if_absent(key, value,
                                                         unit=_pg_unit())
        return str(written["row"]["value"])

    def run(connection: sqlite3.Connection) -> str:
        connection.execute(
            "INSERT INTO auth_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO NOTHING", (key, value))
        row = connection.execute(
            "SELECT value FROM auth_settings WHERE key = ?", (key,)).fetchone()
        return str(row[0])

    return await _off_loop(run)


# ---------------------------------------------------------------- sso bindings

async def get_sso_binding(*, provider: str, subject: str) -> Row | None:
    if _engine == "postgres":
        from hive_data import core

        return await core.get_sso_binding(provider=provider, subject=subject,
                                          unit=_pg_unit())
    return await _one(
        "SELECT provider, subject, username, created_at FROM sso_bindings"
        " WHERE provider = ? AND subject = ?", (provider, subject))


async def put_sso_binding(*, provider: str, subject: str, username: str,
                          created_at: int) -> int:
    if _engine == "postgres":
        from hive_data import core

        written = await core.put_sso_binding(
            provider=provider, subject=subject, username=username,
            created_at=created_at, unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "INSERT INTO sso_bindings (provider, subject, username, created_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT(provider, subject) DO UPDATE SET"
        " username = excluded.username", (provider, subject, username,
                                          created_at))


async def sso_usernames(provider: str) -> set[str]:
    if _engine == "postgres":
        from hive_data import core

        return set(await core.sso_usernames(provider, unit=_pg_unit()))
    rows = await _all(
        "SELECT DISTINCT username FROM sso_bindings WHERE provider = ?",
        (provider,))
    return {row["username"] for row in rows}


async def delete_sso_binding(*, provider: str, username: str) -> int:
    if _engine == "postgres":
        from hive_data import core

        written = await core.delete_sso_binding(provider=provider,
                                                username=username,
                                                unit=_pg_unit())
        return int(written["rows"])
    return await _write(
        "DELETE FROM sso_bindings WHERE provider = ? AND username = ?",
        (provider, username))


# -------------------------------------------------------------- memory browser

class UnknownReference(RuntimeError):
    """A write named a row another table does not hold.

    The one database has real foreign keys where the split files could not,
    so a reference that used to be stored and simply point nowhere is now
    refused at the write. Raised as this module's own class rather than
    `hive_data`'s, so a caller catching it does not have to import the access
    layer — which on the SQLite half is not installed at all.
    """


class MemoryUnavailable(RuntimeError):
    """The memory store is not reachable from this console.

    Its own exception rather than a 503 built here, because this module
    answers questions and does not write HTTP: on the SQLite half it means
    lucent's file is not mounted, which is a deployment the operator has to
    look at, and folding that into an empty listing would report a store with
    no tables.
    """


async def _off_loop_lucent(run: Callable[[sqlite3.Connection], T]) -> T:
    """Run one read of lucent's own file, on a thread of its own.

    Not through `_off_loop`: that opens the console's file and, inside a unit
    of work, would run on the unit's connection and its thread. This is a
    different database, read-only, and nothing about it belongs in a
    transaction over the console's own rows.
    """
    def once() -> T:
        connection = _open_lucent_readonly()
        try:
            return run(connection)
        finally:
            connection.close()

    return await asyncio.to_thread(once)


def _open_lucent_readonly() -> sqlite3.Connection:
    path = lucent_path()
    if not os.path.exists(path):
        raise MemoryUnavailable("lucent database not mounted")
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=0", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _memory_cell(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<BLOB {len(bytes(value))} bytes>"
    return value


async def memory_tables() -> list[Row]:
    """The memory store's tables, each with how many rows it holds."""
    if _engine == "postgres":
        from hive_data import memory

        return await memory.tables(unit=_pg_unit())

    def run(connection: sqlite3.Connection) -> list[Row]:
        names = [r["name"] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
        out = []
        for name in names:
            count = connection.execute(
                f'SELECT COUNT(*) AS c FROM "{name}"').fetchone()
            out.append({"name": name, "row_count": int(count["c"])})
        return out

    return await _off_loop_lucent(run)


async def memory_rows(table: str, *, limit: int = MEMORY_LIMIT_DEFAULT,
                      offset: int = 0) -> Row | None:
    """One page of a memory table, or None when there is no such table.

    None is what the console turns into a 404: a name that matches nothing is
    a typo in a URL, not a failure of the store.
    """
    if _engine == "postgres":
        from hive_data import memory

        return await memory.rows(table, limit=limit, offset=offset,
                                 unit=_pg_unit())
    if not _TABLE_NAME_RE.match(table):
        return None
    safe_limit = max(1, min(int(limit), MEMORY_LIMIT_MAX))
    safe_offset = max(0, int(offset))

    def run(connection: sqlite3.Connection) -> Row | None:
        names = {r["name"] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'").fetchall()}
        if table not in names:
            return None
        columns = [r["name"] for r in connection.execute(
            f'PRAGMA table_info("{table}")').fetchall()]
        total = int(connection.execute(
            f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"])
        rows = [
            {column: _memory_cell(row[column]) for column in columns}
            for row in connection.execute(
                f'SELECT * FROM "{table}" LIMIT ? OFFSET ?',
                (safe_limit, safe_offset)).fetchall()
        ]
        return {
            "table": table, "columns": columns, "rows": rows,
            "total": total, "limit": safe_limit, "offset": safe_offset,
        }

    return await _off_loop_lucent(run)
