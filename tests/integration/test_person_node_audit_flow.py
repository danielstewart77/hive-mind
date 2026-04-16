"""Integration tests for the audit-to-update person node flow.

Tests exercise the full call chain: audit_person_nodes -> update_person_names -> search_person.
Updated for Lucent (SQLite) backend.
"""

import json
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


class TestAuditThenUpdateFlow:
    """End-to-end flow: audit finds nodes, then update sets names."""

    def test_audit_finds_nodes_then_update_sets_names(self) -> None:
        """audit_person_nodes returns incomplete nodes; update_person_names patches them."""
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            # Create nodes without first_name/last_name
            lg.graph_upsert_direct(
                entity_type="Person", name="David Stewart", data_class="person",
                agent_id="ada", source="user",
            )
            lg.graph_upsert_direct(
                entity_type="Person", name="Jane Smith", data_class="person",
                agent_id="ada", source="user",
            )

            # Step 1: Audit
            audit_str = lg.audit_person_nodes(agent_id="ada")
            audit = json.loads(audit_str)

            assert audit["found"] is True
            assert audit["count"] == 2

            # Step 2: Update the first node
            update_str = lg.update_person_names(
                name="David Stewart",
                first_name="David",
                last_name="Stewart",
                agent_id="ada",
            )
            update = json.loads(update_str)

            assert update["updated"] is True
            assert update["first_name"] == "David"
            assert update["last_name"] == "Stewart"

        # Verify in DB
        row = conn.execute(
            "SELECT first_name, last_name FROM nodes WHERE name = 'David Stewart'"
        ).fetchone()
        assert row["first_name"] == "David"
        assert row["last_name"] == "Stewart"

    def test_updated_node_discoverable_by_search_person(self) -> None:
        """After update_person_names, search_person should find the node by first/last name."""
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="David Stewart", data_class="person",
                agent_id="ada", source="user",
            )
            lg.update_person_names(
                name="David Stewart",
                first_name="David",
                last_name="Stewart",
                agent_id="ada",
            )

            # Now search should find it
            search_str = lg.search_person(
                first_name="David",
                last_name="Stewart",
                agent_id="ada",
            )
            search = json.loads(search_str)

        assert search["found"] is True
        assert search["count"] == 1
        assert search["matches"][0]["first_name"] == "David"
        assert search["matches"][0]["last_name"] == "Stewart"
