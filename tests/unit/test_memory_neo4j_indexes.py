"""Unit tests for Lucent SQLite index creation.

Verifies that the Lucent schema creates the expected indexes
on the nodes, edges, and memories tables.
"""

import sqlite3
from unittest.mock import patch

import pytest


def _make_test_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with Lucent schema."""
    import tools.stateful.lucent as lucent_mod

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    lucent_mod._init_schema(conn)
    return conn


class TestLucentIndexes:
    """Tests for index creation in lucent.py."""

    def test_memories_agent_id_index(self) -> None:
        conn = _make_test_conn()
        indexes = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_memories_agent_id" in indexes

    def test_memories_data_class_index(self) -> None:
        conn = _make_test_conn()
        indexes = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_memories_data_class" in indexes

    def test_memories_expires_at_index(self) -> None:
        conn = _make_test_conn()
        indexes = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_memories_expires_at" in indexes

    def test_nodes_agent_id_index(self) -> None:
        conn = _make_test_conn()
        indexes = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_nodes_agent_id" in indexes

    def test_nodes_type_index(self) -> None:
        conn = _make_test_conn()
        indexes = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_nodes_type" in indexes

    def test_schema_idempotent(self) -> None:
        import tools.stateful.lucent as lucent_mod

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        lucent_mod._init_schema(conn)
        # Call again -- should not raise
        lucent_mod._init_schema(conn)
        idx_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
        ).fetchone()[0]
        assert idx_count >= 8
