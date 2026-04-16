"""Integration tests for the full disambiguation and orphan guard flow.

Tests exercise the complete call chain: graph_upsert -> core.kg_guards -> SQLite.
Updated for Lucent (SQLite) backend.
"""

import json
import sqlite3
import time
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


class TestDisambiguationBlocksWriteFlow:
    """Full flow: graph_upsert called with a name that has a similar existing node."""

    def test_disambiguation_blocks_write_and_sends_telegram(self) -> None:
        """When similar node exists, write should be rejected and Telegram message sent."""
        conn = _make_test_conn()

        # Pre-insert a similar node
        conn.execute(
            "INSERT INTO nodes (agent_id, type, name) VALUES (?, ?, ?)",
            ("ada", "Person", "Daniel Stewart"),
        )
        conn.commit()

        import tools.stateful.lucent_graph as lg
        from core import kg_guards

        with (
            _patch_conn(conn),
            patch.object(kg_guards, "_telegram_direct", return_value=(True, "sent")) as mock_tg,
        ):
            result_str = lg.graph_upsert(
                entity_type="Person",
                name="Daniel",
                data_class="person",
                agent_id="ada",
                source="user",
                relation="MANAGES",
                target_name="Hive Mind",
                target_type="Project",
            )
            result = json.loads(result_str)

        assert result["upserted"] is False
        assert result["reason"] == "disambiguation_required"
        assert len(result["similar_nodes"]) == 1
        mock_tg.assert_called_once()
        call_msg = mock_tg.call_args[0][0]
        assert "Daniel" in call_msg
        assert "Daniel Stewart" in call_msg


class TestOrphanGuardBlocksGraphUpsert:
    """Full flow: graph_upsert called without relation/target."""

    def test_orphan_guard_blocks_graph_upsert_without_edges(self) -> None:
        """Write should be rejected with correct error message when no edges."""
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result_str = lg.graph_upsert(
                entity_type="Person",
                name="Daniel",
                data_class="person",
                agent_id="ada",
                source="user",
                relation="",
                target_name="",
            )
            result = json.loads(result_str)

        assert result["upserted"] is False
        assert "Cannot create a node without at least one edge" in result["reason"]


class TestGracePeriodAllowsOrphanViaDirect:
    """graph_upsert_direct without relation succeeds (epilogue use), and created_at is set."""

    def test_grace_period_allows_temporary_orphan_via_direct(self) -> None:
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        before = time.time()

        with _patch_conn(conn):
            result_str = lg.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
                data_class="person",
                agent_id="ada",
                source="session",
            )
            result = json.loads(result_str)

        after = time.time()

        assert result["upserted"] is True

        # Verify created_at timestamp is set
        row = conn.execute(
            "SELECT created_at FROM nodes WHERE name = 'Daniel'"
        ).fetchone()
        assert row is not None
        assert before <= row["created_at"] <= after
