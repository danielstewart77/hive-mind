"""Unit tests for Lucent core module -- SQLite schema and connection management."""

import sqlite3
from unittest.mock import patch

import pytest


class TestGetConnection:
    """Tests for _get_connection() lazy singleton."""

    def test_get_connection_returns_sqlite_connection(self, tmp_path):
        """Asserts _get_connection() returns a sqlite3.Connection object."""
        import tools.stateful.lucent as lucent_mod

        db_path = str(tmp_path / "test.db")
        with patch.object(lucent_mod, "DB_PATH", db_path):
            lucent_mod._conn = None
            try:
                conn = lucent_mod._get_connection()
                assert isinstance(conn, sqlite3.Connection)
            finally:
                lucent_mod._conn = None

    def test_get_connection_lazy_singleton(self, tmp_path):
        """Asserts calling _get_connection() twice returns the same object."""
        import tools.stateful.lucent as lucent_mod

        db_path = str(tmp_path / "test.db")
        with patch.object(lucent_mod, "DB_PATH", db_path):
            lucent_mod._conn = None
            try:
                conn1 = lucent_mod._get_connection()
                conn2 = lucent_mod._get_connection()
                assert conn1 is conn2
            finally:
                lucent_mod._conn = None


class TestInitSchema:
    """Tests for _init_schema() table creation."""

    def test_init_schema_creates_nodes_table(self, tmp_path):
        """Asserts nodes table exists after init with correct columns."""
        import tools.stateful.lucent as lucent_mod

        db_path = str(tmp_path / "test.db")
        with patch.object(lucent_mod, "DB_PATH", db_path):
            lucent_mod._conn = None
            try:
                conn = lucent_mod._get_connection()
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'"
                )
                assert cursor.fetchone() is not None

                # Check columns
                cursor = conn.execute("PRAGMA table_info(nodes)")
                columns = {row[1] for row in cursor.fetchall()}
                expected = {
                    "id", "agent_id", "type", "name", "first_name", "last_name",
                    "properties", "data_class", "tier", "source", "as_of",
                    "created_at", "updated_at",
                }
                assert expected.issubset(columns)
            finally:
                lucent_mod._conn = None

    def test_init_schema_creates_edges_table(self, tmp_path):
        """Asserts edges table exists after init with correct columns."""
        import tools.stateful.lucent as lucent_mod

        db_path = str(tmp_path / "test.db")
        with patch.object(lucent_mod, "DB_PATH", db_path):
            lucent_mod._conn = None
            try:
                conn = lucent_mod._get_connection()
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='edges'"
                )
                assert cursor.fetchone() is not None

                cursor = conn.execute("PRAGMA table_info(edges)")
                columns = {row[1] for row in cursor.fetchall()}
                expected = {
                    "id", "agent_id", "source_id", "target_id", "type",
                    "as_of", "source", "data_class", "tier", "created_at",
                }
                assert expected.issubset(columns)
            finally:
                lucent_mod._conn = None

    def test_init_schema_creates_memories_table(self, tmp_path):
        """Asserts memories table exists after init with correct columns."""
        import tools.stateful.lucent as lucent_mod

        db_path = str(tmp_path / "test.db")
        with patch.object(lucent_mod, "DB_PATH", db_path):
            lucent_mod._conn = None
            try:
                conn = lucent_mod._get_connection()
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
                )
                assert cursor.fetchone() is not None

                cursor = conn.execute("PRAGMA table_info(memories)")
                columns = {row[1] for row in cursor.fetchall()}
                expected = {
                    "id", "agent_id", "content", "embedding", "tags", "source",
                    "data_class", "tier", "as_of", "expires_at", "superseded",
                    "recurring", "codebase_ref", "created_at",
                }
                assert expected.issubset(columns)
            finally:
                lucent_mod._conn = None

    def test_init_schema_creates_indexes(self, tmp_path):
        """Asserts expected indexes are created."""
        import tools.stateful.lucent as lucent_mod

        db_path = str(tmp_path / "test.db")
        with patch.object(lucent_mod, "DB_PATH", db_path):
            lucent_mod._conn = None
            try:
                conn = lucent_mod._get_connection()
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
                indexes = {row[0] for row in cursor.fetchall()}
                expected_indexes = {
                    "idx_nodes_agent_id",
                    "idx_nodes_type",
                    "idx_nodes_first_name",
                    "idx_nodes_last_name",
                    "idx_edges_agent_id",
                    "idx_memories_agent_id",
                    "idx_memories_expires_at",
                    "idx_memories_data_class",
                }
                assert expected_indexes.issubset(indexes), (
                    f"Missing indexes: {expected_indexes - indexes}"
                )
            finally:
                lucent_mod._conn = None

    def test_init_schema_idempotent(self, tmp_path):
        """Asserts calling _init_schema() twice does not raise."""
        import tools.stateful.lucent as lucent_mod

        db_path = str(tmp_path / "test.db")
        with patch.object(lucent_mod, "DB_PATH", db_path):
            lucent_mod._conn = None
            try:
                conn = lucent_mod._get_connection()
                # Call _init_schema again -- should not raise
                lucent_mod._init_schema(conn)
            finally:
                lucent_mod._conn = None

    def test_wal_mode_enabled(self, tmp_path):
        """Asserts the connection uses WAL journal mode."""
        import tools.stateful.lucent as lucent_mod

        db_path = str(tmp_path / "test.db")
        with patch.object(lucent_mod, "DB_PATH", db_path):
            lucent_mod._conn = None
            try:
                conn = lucent_mod._get_connection()
                cursor = conn.execute("PRAGMA journal_mode")
                mode = cursor.fetchone()[0]
                assert mode == "wal"
            finally:
                lucent_mod._conn = None
