"""Unit tests for Lucent graph module -- knowledge graph operations via SQLite."""

import json
import sqlite3
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helper: create an in-memory Lucent DB for tests
# ---------------------------------------------------------------------------

def _make_test_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with Lucent schema."""
    import tools.stateful.lucent as lucent_mod

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    lucent_mod._init_schema(conn)
    return conn


def _patch_conn(conn):
    """Patch _get_connection to return the test connection."""
    return patch("tools.stateful.lucent._get_connection", return_value=conn)


# ---------------------------------------------------------------------------
# graph_upsert_direct tests
# ---------------------------------------------------------------------------
class TestGraphUpsertDirect:
    """Tests for graph_upsert_direct in lucent_graph."""

    def test_creates_node(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
            ))
        assert result["upserted"] is True
        assert result["name"] == "Daniel"
        assert result["entity_type"] == "Person"

    def test_merges_existing_node(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user", properties='{"role": "owner"}',
            )
            result = json.loads(lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user", properties='{"role": "admin"}',
            ))
        assert result["upserted"] is True
        # Only one node should exist
        row = conn.execute("SELECT COUNT(*) FROM nodes WHERE name='Daniel'").fetchone()
        assert row[0] == 1

    def test_creates_edge(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
                relation="MANAGES", target_name="Hive Mind", target_type="Project",
            ))
        assert result["relation_created"] is True
        assert "MANAGES" in result["relation"]

    def test_invalid_entity_type(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_upsert_direct(
                entity_type="Widget", name="X", data_class="person",
                agent_id="ada", source="user",
            ))
        assert "error" in result

    def test_invalid_relation_format(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
                relation="manages", target_name="X",
            ))
        assert "error" in result

    def test_invalid_source(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="random",
            ))
        assert "error" in result

    def test_invalid_data_class(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="unknown-class",
                agent_id="ada", source="user",
            ))
        assert "error" in result


class TestGraphUpsertWithHITL:
    """Tests for graph_upsert with HITL gate and disambiguation."""

    def test_hitl_approved(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg
        from nervous_system.lucent_api.kg_guards import DisambiguationResult

        proceed = DisambiguationResult(action="proceed", existing_nodes=[], message="ok")
        with (
            _patch_conn(conn),
            patch.object(lg, "_hitl_gate", return_value=True),
            patch("core.kg_guards.check_disambiguation", return_value=proceed),
        ):
            result = json.loads(lg.graph_upsert(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
                relation="MANAGES", target_name="Hive Mind", target_type="Project",
            ))
        assert result["upserted"] is True

    def test_hitl_denied(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg
        from nervous_system.lucent_api.kg_guards import DisambiguationResult

        proceed = DisambiguationResult(action="proceed", existing_nodes=[], message="ok")
        with (
            _patch_conn(conn),
            patch.object(lg, "_hitl_gate", return_value=False),
            patch("core.kg_guards.check_disambiguation", return_value=proceed),
        ):
            result = json.loads(lg.graph_upsert(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
                relation="MANAGES", target_name="Hive Mind", target_type="Project",
            ))
        assert result["upserted"] is False
        assert result["reason"] == "denied by HITL"

    def test_disambiguation_required(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg
        from nervous_system.lucent_api.kg_guards import DisambiguationResult

        disambig = DisambiguationResult(
            action="disambiguate",
            existing_nodes=[{"name": "Daniel Stewart", "labels": ["Person"], "id": "1"}],
            message="Similar nodes found",
        )
        with (
            _patch_conn(conn),
            patch("core.kg_guards.check_disambiguation", return_value=disambig),
            patch("core.kg_guards.send_disambiguation_message", return_value=True),
        ):
            result = json.loads(lg.graph_upsert(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
                relation="MANAGES", target_name="Hive Mind", target_type="Project",
            ))
        assert result["upserted"] is False
        assert result["reason"] == "disambiguation_required"


# ---------------------------------------------------------------------------
# graph_query tests
# ---------------------------------------------------------------------------
class TestGraphQuery:
    """Tests for graph_query."""

    def test_exact_match(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
            )
            result = json.loads(lg.graph_query("Daniel", "ada"))
        assert result["found"] is True
        assert result["count"] == 1

    def test_first_name_match(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel Stewart", data_class="person",
                agent_id="ada", source="user",
                properties='{"first_name": "Daniel"}',
            )
            # Also set first_name column directly
            conn.execute("UPDATE nodes SET first_name='Daniel' WHERE name='Daniel Stewart'")
            conn.commit()
            result = json.loads(lg.graph_query("Daniel", "ada"))
        assert result["found"] is True

    def test_last_name_match(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel Stewart", data_class="person",
                agent_id="ada", source="user",
            )
            conn.execute("UPDATE nodes SET last_name='Stewart' WHERE name='Daniel Stewart'")
            conn.commit()
            result = json.loads(lg.graph_query("Stewart", "ada"))
        assert result["found"] is True

    def test_alias_match(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
                properties='{"aliases": ["Dan", "Danny"]}',
            )
            result = json.loads(lg.graph_query("Dan", "ada"))
        assert result["found"] is True

    def test_not_found(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_query("Nobody", "ada"))
        assert result["found"] is False

    def test_with_depth(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
                relation="MANAGES", target_name="Hive Mind", target_type="Project",
            )
            result = json.loads(lg.graph_query("Daniel", "ada", depth=2))
        assert result["found"] is True
        # Should have connections from the edge
        match = result["matches"][0]
        assert len(match["connections"]) >= 1

    def test_depth_clamped(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
            )
            # depth=0 should be clamped to 1, depth=10 to 3
            r1 = json.loads(lg.graph_query("Daniel", "ada", depth=0))
            r2 = json.loads(lg.graph_query("Daniel", "ada", depth=10))
        assert r1["found"] is True
        assert r2["found"] is True


# ---------------------------------------------------------------------------
# search_person tests
# ---------------------------------------------------------------------------
class TestSearchPerson:
    """Tests for search_person."""

    def test_by_first_name(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel Stewart", data_class="person",
                agent_id="ada", source="user",
            )
            conn.execute(
                "UPDATE nodes SET first_name='Daniel' WHERE name='Daniel Stewart'"
            )
            conn.commit()
            result = json.loads(lg.search_person(first_name="Dan", agent_id="ada"))
        assert result["found"] is True

    def test_by_last_name(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel Stewart", data_class="person",
                agent_id="ada", source="user",
            )
            conn.execute(
                "UPDATE nodes SET last_name='Stewart' WHERE name='Daniel Stewart'"
            )
            conn.commit()
            result = json.loads(lg.search_person(last_name="Stew", agent_id="ada"))
        assert result["found"] is True

    def test_by_title(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Dr. Smith", data_class="person",
                agent_id="ada", source="user",
                properties='{"title": "Doctor"}',
            )
            result = json.loads(lg.search_person(title="doc", agent_id="ada"))
        assert result["found"] is True

    def test_by_relationship(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Jane", data_class="person",
                agent_id="ada", source="user",
                properties='{"relationship": ["wife"]}',
            )
            result = json.loads(lg.search_person(relationship="wife", agent_id="ada"))
        assert result["found"] is True

    def test_no_params_returns_error(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.search_person(agent_id="ada"))
        assert "error" in result

    def test_not_found(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.search_person(first_name="Nobody", agent_id="ada"))
        assert result["found"] is False

    def test_combined_filters(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel Stewart", data_class="person",
                agent_id="ada", source="user",
            )
            conn.execute(
                "UPDATE nodes SET first_name='Daniel', last_name='Stewart' WHERE name='Daniel Stewart'"
            )
            conn.commit()
            result = json.loads(lg.search_person(
                first_name="Daniel", last_name="Stewart", agent_id="ada",
            ))
        assert result["found"] is True
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# audit_person_nodes tests
# ---------------------------------------------------------------------------
class TestAuditPersonNodes:
    """Tests for audit_person_nodes."""

    def test_finds_missing_names(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel Stewart", data_class="person",
                agent_id="ada", source="user",
            )
            result = json.loads(lg.audit_person_nodes(agent_id="ada"))
        # first_name and last_name are NULL by default
        assert result["found"] is True
        assert result["count"] == 1

    def test_all_complete(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel Stewart", data_class="person",
                agent_id="ada", source="user",
            )
            conn.execute(
                "UPDATE nodes SET first_name='Daniel', last_name='Stewart' WHERE name='Daniel Stewart'"
            )
            conn.commit()
            result = json.loads(lg.audit_person_nodes(agent_id="ada"))
        assert result["found"] is False
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# update_person_names tests
# ---------------------------------------------------------------------------
class TestUpdatePersonNames:
    """Tests for update_person_names."""

    def test_sets_first_name(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
            )
            result = json.loads(lg.update_person_names(
                name="Daniel", first_name="Daniel", agent_id="ada",
            ))
        assert result["updated"] is True
        assert result["first_name"] == "Daniel"

    def test_sets_last_name(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            lg.graph_upsert_direct(
                entity_type="Person", name="Daniel", data_class="person",
                agent_id="ada", source="user",
            )
            result = json.loads(lg.update_person_names(
                name="Daniel", last_name="Stewart", agent_id="ada",
            ))
        assert result["updated"] is True
        assert result["last_name"] == "Stewart"

    def test_no_params_error(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.update_person_names(
                name="Daniel", agent_id="ada",
            ))
        assert "error" in result

    def test_not_found(self):
        conn = _make_test_conn()
        import tools.stateful.lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.update_person_names(
                name="Nobody", first_name="N", agent_id="ada",
            ))
        assert result["updated"] is False


# ---------------------------------------------------------------------------
# KG_TOOLS list test
# ---------------------------------------------------------------------------
class TestKGToolsList:
    """Test that KG_TOOLS contains the expected functions."""

    def test_kg_tools_list_matches_original(self):
        import tools.stateful.lucent_graph as lg

        expected_names = {
            "graph_upsert", "graph_upsert_direct", "graph_query",
            "search_person", "audit_person_nodes", "update_person_names",
        }
        actual_names = {f.__name__ for f in lg.KG_TOOLS}
        assert expected_names == actual_names
