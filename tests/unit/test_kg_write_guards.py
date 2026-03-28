"""Unit tests for guard integration in graph_upsert and graph_upsert_direct.

Tests that disambiguation and orphan guard are called correctly in the
HITL-gated path (graph_upsert) and the epilogue path (graph_upsert_direct).
"""

import json
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from core.kg_guards import DisambiguationResult


@pytest.fixture(autouse=True)
def _mock_neo4j_and_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure agents.knowledge_graph can be imported by mocking neo4j and agent_tooling."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)


def _make_mock_driver() -> MagicMock:
    """Create a mock Neo4j driver with session and run mocked."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    mock_result = MagicMock()
    mock_result.single.return_value = {"id": "test-node-id"}
    mock_session.run.return_value = mock_result
    return mock_driver


# ---------------------------------------------------------------------------
# graph_upsert (HITL-gated path) tests — Step 4
# ---------------------------------------------------------------------------
class TestGraphUpsertGuards:
    """Tests for guard integration in graph_upsert."""

    def test_graph_upsert_calls_disambiguation_before_write(self) -> None:
        """check_disambiguation should be called before _hitl_gate."""
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        proceed_result = DisambiguationResult(
            action="proceed", existing_nodes=[], message="No match."
        )

        with (
            patch.object(kg_mod, "_hitl_gate", return_value=True) as mock_hitl,
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
            patch("core.kg_guards.check_disambiguation", return_value=proceed_result) as mock_disambig,
        ):
            kg_mod.graph_upsert(
                entity_type="Person",
                name="Daniel",
                data_class="person",
                agent_id="ada",
                source="user",
                relation="MANAGES",
                target_name="Hive Mind",
                target_type="Project",
            )

        mock_disambig.assert_called_once_with("Daniel", "Person", "ada")
        mock_hitl.assert_called_once()

    def test_graph_upsert_exact_match_proceeds_to_merge(self) -> None:
        """When disambiguation returns 'merge', write should proceed (MERGE Cypher handles it)."""
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        merge_result = DisambiguationResult(
            action="merge",
            existing_nodes=[{"name": "Daniel", "labels": ["Person"], "id": "n1"}],
            message="Exact match.",
        )

        with (
            patch.object(kg_mod, "_hitl_gate", return_value=True),
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
            patch("core.kg_guards.check_disambiguation", return_value=merge_result),
        ):
            result_str = kg_mod.graph_upsert(
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

        assert result["upserted"] is True

    def test_graph_upsert_similar_name_sends_disambiguation_and_rejects(self) -> None:
        """When disambiguation returns 'disambiguate', write should be rejected."""
        import tools.stateful.knowledge_graph as kg_mod

        disambig_result = DisambiguationResult(
            action="disambiguate",
            existing_nodes=[{"name": "Daniel Stewart", "labels": ["Person"], "id": "n1"}],
            message="Similar found.",
        )

        with (
            patch("core.kg_guards.check_disambiguation", return_value=disambig_result),
            patch("core.kg_guards.send_disambiguation_message", return_value=True) as mock_send,
        ):
            result_str = kg_mod.graph_upsert(
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
        mock_send.assert_called_once()

    def test_graph_upsert_no_match_proceeds_normally(self) -> None:
        """When no similar node found, write should proceed normally."""
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        proceed_result = DisambiguationResult(
            action="proceed", existing_nodes=[], message="No match."
        )

        with (
            patch.object(kg_mod, "_hitl_gate", return_value=True),
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
            patch("core.kg_guards.check_disambiguation", return_value=proceed_result),
        ):
            result_str = kg_mod.graph_upsert(
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

        assert result["upserted"] is True

    def test_graph_upsert_orphan_guard_rejects_no_edges(self) -> None:
        """Write should be rejected when no relation/target provided."""
        import tools.stateful.knowledge_graph as kg_mod

        result_str = kg_mod.graph_upsert(
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

    def test_graph_upsert_orphan_guard_allows_with_edges(self) -> None:
        """Write should proceed when relation and target_name provided."""
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        proceed_result = DisambiguationResult(
            action="proceed", existing_nodes=[], message="No match."
        )

        with (
            patch.object(kg_mod, "_hitl_gate", return_value=True),
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
            patch("core.kg_guards.check_disambiguation", return_value=proceed_result),
        ):
            result_str = kg_mod.graph_upsert(
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

        assert result["upserted"] is True

    def test_graph_upsert_disambiguation_result_in_response(self) -> None:
        """Returned JSON should include disambiguation info when rejected."""
        import tools.stateful.knowledge_graph as kg_mod

        disambig_result = DisambiguationResult(
            action="disambiguate",
            existing_nodes=[{"name": "Daniel Stewart", "labels": ["Person"], "id": "n1"}],
            message="Similar found.",
        )

        with (
            patch("core.kg_guards.check_disambiguation", return_value=disambig_result),
            patch("core.kg_guards.send_disambiguation_message", return_value=True),
        ):
            result_str = kg_mod.graph_upsert(
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

        assert "similar_nodes" in result
        assert len(result["similar_nodes"]) == 1
        assert result["similar_nodes"][0]["name"] == "Daniel Stewart"

    def test_graph_upsert_orphan_rejection_returns_json_error(self) -> None:
        """Returned JSON should contain the orphan error message."""
        import tools.stateful.knowledge_graph as kg_mod

        result_str = kg_mod.graph_upsert(
            entity_type="Person",
            name="Daniel",
            data_class="person",
            agent_id="ada",
            source="user",
        )
        result = json.loads(result_str)

        assert result["upserted"] is False
        assert "Cannot create a node without at least one edge" in result["reason"]


# ---------------------------------------------------------------------------
# graph_upsert_direct (epilogue path) tests — Step 5
# ---------------------------------------------------------------------------
class TestGraphUpsertDirectGuards:
    """Tests for guard integration in graph_upsert_direct."""

    def test_graph_upsert_direct_with_relation_proceeds(self) -> None:
        """Write should succeed when relation and target are provided."""
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        with (
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
        ):
            result_str = kg_mod.graph_upsert_direct(
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

        assert result["upserted"] is True
        assert result["relation_created"] is True

    def test_graph_upsert_direct_without_relation_uses_grace_period(self) -> None:
        """graph_upsert_direct should pass through without relation (grace period for epilogue)."""
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        with (
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
        ):
            result_str = kg_mod.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
                data_class="person",
                agent_id="ada",
                source="session",
            )
            result = json.loads(result_str)

        assert result["upserted"] is True

    def test_graph_upsert_direct_adds_created_at_to_node(self) -> None:
        """A created_at epoch timestamp should be added to node properties."""
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        before = time.time()

        with (
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
        ):
            kg_mod.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
                data_class="person",
                agent_id="ada",
                source="user",
                relation="MANAGES",
                target_name="Hive Mind",
                target_type="Project",
            )

        after = time.time()

        # Check the props dict passed to SET n += $props
        mock_session = mock_driver.session.return_value.__enter__.return_value
        call_args = mock_session.run.call_args_list[0]
        params = call_args[1]
        props = params["props"]
        assert "created_at" in props
        assert before <= props["created_at"] <= after
