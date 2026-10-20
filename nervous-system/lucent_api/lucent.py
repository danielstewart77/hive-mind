"""Lucent -- SQLite-backed graph and vector store.

Replaces Neo4j with a single-file SQLite database for knowledge graph
and vector memory storage. Provides connection management and schema
initialization used by lucent_graph.py and lucent_memory.py.

Schema: nodes, edges, memories tables with indexes on frequently queried columns.
Connection: per-thread, lazy, with WAL mode. Each thread gets its own
connection so concurrent reads and writes don't share cursor state — a
shared sqlite3.Connection with check_same_thread=False is documented as
"safe" only when callers serialize access themselves, and uvicorn's
threadpool does not.
"""

import os
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "lucent.db")

_thread_local = threading.local()
_schema_init_lock = threading.Lock()
_schema_initialized = False


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they do not already exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Which mind wrote the row. Still recorded, and still the whole
            -- of the identity rule: a `Mind` node's `mind_id` is the mind it
            -- represents, which is what lets `_check_identity_guard` refuse
            -- every other mind. It is no longer an ownership claim.
            mind_id    TEXT    NOT NULL,
            -- Whose graph this node is in. The partition every read and
            -- every write is scoped by. Null means no owner could be
            -- established, which only a service or an admin reaches.
            owner       TEXT,
            type        TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            first_name  TEXT,
            last_name   TEXT,
            properties  TEXT    DEFAULT '{}',
            data_class  TEXT,
            tier        TEXT,
            source      TEXT,
            as_of       TEXT,
            created_at  REAL,
            updated_at  REAL,
            UNIQUE(owner, type, name)
        );

        CREATE TABLE IF NOT EXISTS edges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mind_id    TEXT    NOT NULL,
            owner       TEXT,
            source_id   INTEGER NOT NULL REFERENCES nodes(id),
            target_id   INTEGER NOT NULL REFERENCES nodes(id),
            type        TEXT    NOT NULL,
            as_of       TEXT,
            source      TEXT,
            data_class  TEXT,
            tier        TEXT,
            created_at  REAL,
            properties  TEXT    NOT NULL DEFAULT '{}',
            UNIQUE(source_id, target_id, type)
        );

        CREATE TABLE IF NOT EXISTS memories (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            mind_id      TEXT    NOT NULL,
            content       TEXT    NOT NULL,
            embedding     BLOB,
            tags          TEXT    DEFAULT '',
            source        TEXT,
            data_class    TEXT,
            tier          TEXT,
            as_of         TEXT,
            expires_at    TEXT,
            superseded    INTEGER DEFAULT 0,
            recurring     INTEGER,
            codebase_ref  TEXT,
            created_at    INTEGER
        );

        CREATE TABLE IF NOT EXISTS schema_types (
            name             TEXT    PRIMARY KEY,
            kind             TEXT    NOT NULL CHECK (kind IN ('first-class','second-class')),
            property_schema  TEXT    NOT NULL,
            created_at       REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_edges (
            name             TEXT    PRIMARY KEY,
            source_type      TEXT    NOT NULL,
            target_type      TEXT    NOT NULL,
            attr_schema      TEXT    NOT NULL,
            symmetric        INTEGER NOT NULL DEFAULT 0,
            created_at       REAL    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_nodes_mind_id    ON nodes(mind_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_type         ON nodes(type);
        CREATE INDEX IF NOT EXISTS idx_nodes_first_name   ON nodes(first_name);
        CREATE INDEX IF NOT EXISTS idx_nodes_last_name    ON nodes(last_name);
        CREATE INDEX IF NOT EXISTS idx_edges_mind_id     ON edges(mind_id);
        CREATE INDEX IF NOT EXISTS idx_memories_mind_id  ON memories(mind_id);
        CREATE INDEX IF NOT EXISTS idx_memories_expires_at ON memories(expires_at);
        CREATE INDEX IF NOT EXISTS idx_memories_data_class ON memories(data_class);
    """)

    # Seed the schema tables from the hardcoded registry. Idempotent —
    # INSERT OR IGNORE keyed on name. Rows added later via the admin
    # endpoints are preserved.
    from lucent_api.schema_registry import seed_schema_tables

    seed_schema_tables(conn)

    _migrate_graph_owner(conn)


#: The user every pre-existing node belongs to when its writer cannot be
#: resolved to one. Named by the environment rather than written here: a
#: person's name in shipped code is one install's code.
PRIMARY_USER_ENV = "LUCENT_PRIMARY_USER"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _owner_of_writers(conn: sqlite3.Connection) -> dict[str, str]:
    """Each mind's owner, as lucent's own credential table records it.

    Lucent's credentials carry the user their holder answers for, which is
    the only owner fact this process holds. Comms' `minds.owner` is the
    same fact in another service's database, and reaching into it behind
    comms' back would make two readers of one truth.
    """
    try:
        rows = conn.execute(
            "SELECT holder, username, mind_id FROM credentials WHERE revoked_at IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        # No credentials table yet — a first boot, where there is also
        # nothing on disk to migrate.
        return {}
    owners: dict[str, str] = {}
    for holder, username, mind_id in ((r[0], r[1], r[2]) for r in rows):
        user = str(username or "").strip().lower()
        if not user:
            continue
        for key in (str(mind_id or "").strip(), str(holder or "").strip().lower()):
            if key:
                owners[key] = user
    return owners


def _migrate_graph_owner(conn: sqlite3.Connection) -> None:
    """Give every graph row an owner, once, in the same act as the code.

    The column and the backfill land together because the two halves fail
    in opposite directions on their own: code ahead of data scopes reads to
    an owner no row carries and every lookup answers "not found", while
    data ahead of code leaves the identity guard comparing a uuid against a
    username and refuses every mind its own node. Both report success.

    A node's owner is the owner of the mind that wrote it. A row whose
    writer this store cannot resolve — the console's own provenance-less
    edges, a mind whose credential was never minted here — goes to the
    primary user, and when none is named it is left null rather than
    guessed at: a node handed to the wrong person is worse than one only a
    service can reach.
    """
    owners = _owner_of_writers(conn)
    primary = str(os.getenv(PRIMARY_USER_ENV, "")).strip().lower()

    def owner_for(mind_id: object) -> str | None:
        key = str(mind_id or "").strip()
        return owners.get(key) or owners.get(key.lower()) or primary or None

    # The indexes are created here rather than in the create-tables script:
    # on a database that already has the tables, `CREATE TABLE IF NOT EXISTS`
    # no-ops and an index naming `owner` in the same script would run before
    # the column exists, taking the whole schema init down with it.
    if "owner" not in _columns(conn, "nodes"):
        conn.execute("ALTER TABLE nodes ADD COLUMN owner TEXT")
    if "owner" not in _columns(conn, "edges"):
        conn.execute("ALTER TABLE edges ADD COLUMN owner TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_owner ON nodes(owner)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_owner ON edges(owner)")

    for table in ("nodes", "edges"):
        rows = conn.execute(
            f"SELECT id, mind_id FROM {table} WHERE owner IS NULL"
        ).fetchall()
        for row_id, mind_id in ((r[0], r[1]) for r in rows):
            resolved = owner_for(mind_id)
            if resolved:
                conn.execute(
                    f"UPDATE {table} SET owner = ? WHERE id = ?", (resolved, row_id)
                )
    conn.commit()
    _rekey_nodes_on_owner(conn)


def _rekey_nodes_on_owner(conn: sqlite3.Connection) -> None:
    """Key `nodes` on (owner, type, name).

    Once an owner's minds share one graph the key that mattered was never
    the writer: two of one person's minds writing the same person is one
    node, and keying on the writer made it two that every lookup then chose
    between by row id. The key carries `type` as well, because a name can
    legitimately be both a Place and an Organization.

    A unique index rather than a table rebuild: it reaches an existing table
    where SQLite's inline UNIQUE cannot be altered, and the live store's
    `nodes` was rebuilt without an inline key by an earlier migration, so
    there is nothing to drop.
    """
    clashes = conn.execute(
        "SELECT owner, type, name, COUNT(*) c FROM nodes "
        "GROUP BY owner, type, name HAVING c > 1"
    ).fetchall()
    if clashes:
        # Refused rather than resolved. Merging two rows is a judgement about
        # which properties survive, and a migration that picks silently loses
        # whichever it did not pick with nothing left to notice it by.
        detail = ", ".join(f"{r[0]}/{r[1]}/{r[2]} x{r[3]}" for r in clashes)
        raise RuntimeError(
            "cannot key nodes on (owner, type, name): rows already collide: "
            + detail
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS nodes_owner_type_name "
        "ON nodes(owner, type, name)"
    )
    conn.commit()


def _get_connection() -> sqlite3.Connection:
    """Return a per-thread SQLite connection, initializing schema on first call.

    Each worker thread gets its own connection. WAL mode lets concurrent
    readers and a single writer coexist at the SQLite layer. Schema
    initialization runs exactly once across the whole process, guarded
    by a lock so the first two threads can't race the CREATE TABLE
    statements.
    """
    global _schema_initialized
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        with _schema_init_lock:
            if not _schema_initialized:
                _init_schema(conn)
                _schema_initialized = True
        _thread_local.conn = conn
    return conn
