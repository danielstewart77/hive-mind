"""Lucent -- SQLite-backed graph and vector store.

Replaces Neo4j with a single-file SQLite database for knowledge graph
and vector memory storage. Provides connection management and schema
initialization used by lucent_graph.py and lucent_memory.py.

Schema: nodes, edges, memories tables with indexes on frequently queried columns.
Connection: lazy singleton with WAL mode and check_same_thread=False.
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "lucent.db")

_conn: sqlite3.Connection | None = None


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they do not already exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id    TEXT    NOT NULL,
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
            UNIQUE(agent_id, name)
        );

        CREATE TABLE IF NOT EXISTS edges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id    TEXT    NOT NULL,
            source_id   INTEGER NOT NULL REFERENCES nodes(id),
            target_id   INTEGER NOT NULL REFERENCES nodes(id),
            type        TEXT    NOT NULL,
            as_of       TEXT,
            source      TEXT,
            data_class  TEXT,
            tier        TEXT,
            created_at  REAL,
            UNIQUE(source_id, target_id, type)
        );

        CREATE TABLE IF NOT EXISTS memories (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id      TEXT    NOT NULL,
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

        CREATE INDEX IF NOT EXISTS idx_nodes_agent_id    ON nodes(agent_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_type         ON nodes(type);
        CREATE INDEX IF NOT EXISTS idx_nodes_first_name   ON nodes(first_name);
        CREATE INDEX IF NOT EXISTS idx_nodes_last_name    ON nodes(last_name);
        CREATE INDEX IF NOT EXISTS idx_edges_agent_id     ON edges(agent_id);
        CREATE INDEX IF NOT EXISTS idx_memories_agent_id  ON memories(agent_id);
        CREATE INDEX IF NOT EXISTS idx_memories_expires_at ON memories(expires_at);
        CREATE INDEX IF NOT EXISTS idx_memories_data_class ON memories(data_class);
    """)


def _get_connection() -> sqlite3.Connection:
    """Return the lazy-singleton SQLite connection, initializing schema on first call."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn
