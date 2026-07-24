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
            mind_id    TEXT    NOT NULL,
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
            UNIQUE(mind_id, name)
        );

        CREATE TABLE IF NOT EXISTS edges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mind_id    TEXT    NOT NULL,
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
