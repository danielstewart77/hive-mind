"""Unit tests for kg_guards after Lucent migration -- uses real in-memory SQLite."""

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


def _patch_conn(conn):
    return patch("tools.stateful.lucent._get_connection", return_value=conn)


class TestCheckDisambiguationLucent:
    """Tests for check_disambiguation using SQLite backend."""

    def test_no_match_proceeds(self):
        conn = _make_test_conn()
        from core import kg_guards

        with _patch_conn(conn):
            result = kg_guards.check_disambiguation("NewEntity", "Person", "ada")
        assert result.action == "proceed"
        assert result.existing_nodes == []

    def test_exact_match_merges(self):
        conn = _make_test_conn()
        from core import kg_guards

        # Insert a node
        conn.execute(
            "INSERT INTO nodes (agent_id, type, name) VALUES (?, ?, ?)",
            ("ada", "Person", "Daniel"),
        )
        conn.commit()

        with _patch_conn(conn):
            result = kg_guards.check_disambiguation("Daniel", "Person", "ada")
        assert result.action == "merge"
        assert len(result.existing_nodes) >= 1

    def test_similar_name_disambiguates(self):
        conn = _make_test_conn()
        from core import kg_guards

        conn.execute(
            "INSERT INTO nodes (agent_id, type, name) VALUES (?, ?, ?)",
            ("ada", "Person", "Daniel Stewart"),
        )
        conn.commit()

        with _patch_conn(conn):
            result = kg_guards.check_disambiguation("Daniel", "Person", "ada")
        assert result.action == "disambiguate"
        assert len(result.existing_nodes) >= 1

    def test_cross_type(self):
        conn = _make_test_conn()
        from core import kg_guards

        conn.execute(
            "INSERT INTO nodes (agent_id, type, name) VALUES (?, ?, ?)",
            ("ada", "System", "Hive Mind"),
        )
        conn.commit()

        with _patch_conn(conn):
            result = kg_guards.check_disambiguation("Hive Mind", "Project", "ada")
        # Should find the System node despite looking for Project
        assert result.action == "merge"
        assert len(result.existing_nodes) >= 1
