"""Unit tests for metadata enforcement in graph_upsert and graph_upsert_direct."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


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


class TestGraphUpsertDirectMetadata:
    """Tests for graph_upsert_direct with metadata parameters."""

    def test_graph_upsert_direct_with_data_class_sets_metadata_on_node(self) -> None:
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
            )
            result = json.loads(result_str)
            assert result["upserted"] is True
            assert result.get("data_class") == "person"

            # Check the props dict passed to SET n += $props
            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args_list[0]
            params = call_args[1]
            props = params["props"]
            assert props["data_class"] == "person"
            assert props["tier"] == "durable"
            assert props["superseded"] is False
            assert "as_of" in props
            assert props["source"] == "user"

    def test_graph_upsert_direct_unknown_class_returns_prompt(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        with (
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
        ):
            result_str = kg_mod.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
                data_class="unknown-class",
                agent_id="ada",
            )
            result = json.loads(result_str)
            assert "error" in result
            assert "unknown-class" in result["error"].lower()

    def test_graph_upsert_direct_without_data_class_raises_type_error(self) -> None:
        import tools.stateful.knowledge_graph as kg_mod

        with pytest.raises(TypeError):
            kg_mod.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
            )

    def test_graph_upsert_direct_metadata_on_relationship_target(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        with (
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
        ):
            result_str = kg_mod.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
                relation="MANAGES",
                target_name="Hive Mind",
                target_type="Project",
                data_class="person",
                agent_id="ada",
                source="user",
            )
            result = json.loads(result_str)
            assert result["upserted"] is True
            assert result["relation_created"] is True

            # The second run call should set metadata on the target/relationship
            mock_session = mock_driver.session.return_value.__enter__.return_value
            rel_call = mock_session.run.call_args_list[1]
            rel_query = rel_call[0][0] if rel_call[0] else ""
            rel_params = rel_call[1]
            # Metadata should appear in the params for target node
            assert "meta_data_class" in rel_params or "data_class" in rel_query

    def test_graph_upsert_direct_relationship_includes_tier(self) -> None:
        """AC-4: tier must be set on every edge, not just nodes."""
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        with (
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
        ):
            kg_mod.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
                relation="MANAGES",
                target_name="Hive Mind",
                target_type="Project",
                data_class="person",
                agent_id="ada",
                source="user",
            )

            mock_session = mock_driver.session.return_value.__enter__.return_value
            rel_call = mock_session.run.call_args_list[1]
            rel_query = rel_call[0][0] if rel_call[0] else ""
            rel_params = rel_call[1]
            # r.tier must be set in the relationship SET clause
            assert "r.tier" in rel_query
            assert "meta_tier" in rel_params
            assert rel_params["meta_tier"] == "durable"

    def test_graph_upsert_direct_invalid_source_returns_error(self) -> None:
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
                source="random",
            )
            result = json.loads(result_str)
            assert "error" in result

    def test_graph_upsert_return_includes_data_class(self) -> None:
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
            )
            result = json.loads(result_str)
            assert "data_class" in result
            assert result["data_class"] == "person"


class TestGraphUpsertWithHITL:
    """Tests for graph_upsert (HITL-gated) with metadata pass-through."""

    def test_graph_upsert_with_hitl_passes_data_class_through(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod
        from nervous_system.lucent_api.kg_guards import DisambiguationResult

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
                relation="KNOWS_ABOUT",
                target_name="Hive Mind",
                target_type="Project",
            )
            result = json.loads(result_str)
            assert result["upserted"] is True
            assert result.get("data_class") == "person"
