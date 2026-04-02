"""Unit tests for person node audit tools: audit_person_nodes and update_person_names."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_neo4j_and_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tools.stateful.knowledge_graph can be imported by mocking neo4j."""
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
    mock_result.data.return_value = []
    mock_session.run.return_value = mock_result
    return mock_driver


class TestAuditPersonNodes:
    """Tests for audit_person_nodes query tool."""

    def test_audit_person_nodes_returns_nodes_missing_first_name(self) -> None:
        """Nodes with first_name=null should be returned by audit."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_result = MagicMock()
        mock_result.data.return_value = [
            {
                "n": {"name": "David Stewart", "first_name": None, "last_name": "Stewart", "agent_id": "ada"},
                "element_id": "4:abc:123",
            }
        ]
        mock_session.run.return_value = mock_result

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            result_str = kg_mod.audit_person_nodes(agent_id="ada")
            result = json.loads(result_str)

        assert result["found"] is True
        assert result["count"] == 1
        assert result["nodes"][0]["name"] == "David Stewart"

    def test_audit_person_nodes_returns_nodes_missing_last_name(self) -> None:
        """Nodes with last_name=null should be returned by audit."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_result = MagicMock()
        mock_result.data.return_value = [
            {
                "n": {"name": "Jane", "first_name": "Jane", "last_name": None, "agent_id": "ada"},
                "element_id": "4:abc:456",
            }
        ]
        mock_session.run.return_value = mock_result

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            result_str = kg_mod.audit_person_nodes(agent_id="ada")
            result = json.loads(result_str)

        assert result["found"] is True
        assert result["count"] == 1
        assert result["nodes"][0]["name"] == "Jane"

    def test_audit_person_nodes_excludes_complete_nodes(self) -> None:
        """The Cypher query must filter on NULL first_name or last_name to exclude complete nodes."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            kg_mod.audit_person_nodes(agent_id="ada")

        # Verify the Cypher query contains the WHERE clause that filters out complete nodes
        call_args = mock_session.run.call_args
        query = call_args[0][0] if call_args[0] else call_args[1].get("query", "")
        assert "first_name IS NULL" in query
        assert "last_name IS NULL" in query

    def test_audit_person_nodes_returns_empty_when_all_complete(self) -> None:
        """When no rows are returned (all nodes complete), return found=false."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_result = MagicMock()
        mock_result.data.return_value = []
        mock_session.run.return_value = mock_result

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            result_str = kg_mod.audit_person_nodes(agent_id="ada")
            result = json.loads(result_str)

        assert result["found"] is False
        assert result["count"] == 0
        assert result["nodes"] == []

    def test_audit_person_nodes_includes_existing_properties(self) -> None:
        """Returned nodes should include all existing properties for skill reasoning."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_result = MagicMock()
        mock_result.data.return_value = [
            {
                "n": {
                    "name": "Coach Johnson",
                    "first_name": None,
                    "last_name": None,
                    "title": "Coach",
                    "relationship": ["coach"],
                    "agent_id": "ada",
                },
                "element_id": "4:abc:789",
            }
        ]
        mock_session.run.return_value = mock_result

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            result_str = kg_mod.audit_person_nodes(agent_id="ada")
            result = json.loads(result_str)

        assert result["found"] is True
        node = result["nodes"][0]
        assert node["name"] == "Coach Johnson"
        assert node["element_id"] == "4:abc:789"
        assert "title" in node["properties"]
        assert node["properties"]["title"] == "Coach"
        assert "relationship" in node["properties"]

    def test_audit_person_nodes_handles_driver_error(self) -> None:
        """Driver exceptions should return a JSON error, not crash."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_session.run.side_effect = Exception("Connection refused")

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            result_str = kg_mod.audit_person_nodes(agent_id="ada")
            result = json.loads(result_str)

        assert "error" in result
        assert "Connection refused" in result["error"]


class TestUpdatePersonNames:
    """Tests for update_person_names write tool."""

    def test_update_person_names_sets_first_and_last_name(self) -> None:
        """Should SET first_name and last_name on the matched Person node."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_result = MagicMock()
        mock_result.single.return_value = {"name": "David Stewart"}
        mock_session.run.return_value = mock_result

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            result_str = kg_mod.update_person_names(
                name="David Stewart",
                first_name="David",
                last_name="Stewart",
                agent_id="ada",
            )
            result = json.loads(result_str)

        assert result["updated"] is True
        assert result["name"] == "David Stewart"
        assert result["first_name"] == "David"
        assert result["last_name"] == "Stewart"

        # Verify the Cypher params
        call_args = mock_session.run.call_args
        params = call_args[1]
        assert params["first_name"] == "David"
        assert params["last_name"] == "Stewart"

    def test_update_person_names_requires_agent_id(self) -> None:
        """agent_id is keyword-only and required; omitting it should raise TypeError."""
        import tools.stateful.knowledge_graph as kg_mod

        with pytest.raises(TypeError):
            kg_mod.update_person_names(name="Test", first_name="T")  # type: ignore[call-arg]

    def test_update_person_names_requires_at_least_one_name_field(self) -> None:
        """Calling with neither first_name nor last_name should return an error."""
        import tools.stateful.knowledge_graph as kg_mod

        result_str = kg_mod.update_person_names(
            name="David Stewart",
            agent_id="ada",
        )
        result = json.loads(result_str)

        assert "error" in result

    def test_update_person_names_allows_partial_update(self) -> None:
        """Setting only first_name should not set last_name in the Cypher params."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_result = MagicMock()
        mock_result.single.return_value = {"name": "Jane"}
        mock_session.run.return_value = mock_result

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            result_str = kg_mod.update_person_names(
                name="Jane",
                first_name="Jane",
                agent_id="ada",
            )
            result = json.loads(result_str)

        assert result["updated"] is True
        assert result["first_name"] == "Jane"

        # Verify only first_name is in the SET clause params
        call_args = mock_session.run.call_args
        query = call_args[0][0] if call_args[0] else call_args[1].get("query", "")
        # last_name should NOT be set in the query
        assert "n.first_name" in query
        assert "n.last_name" not in query

    def test_update_person_names_node_not_found_returns_error(self) -> None:
        """When no matching node exists, should return updated=false error."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_result = MagicMock()
        mock_result.single.return_value = None
        mock_session.run.return_value = mock_result

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            result_str = kg_mod.update_person_names(
                name="Nonexistent Person",
                first_name="Non",
                last_name="Existent",
                agent_id="ada",
            )
            result = json.loads(result_str)

        assert result["updated"] is False
        assert "error" in result or "not found" in result.get("reason", "").lower()

    def test_update_person_names_handles_driver_error(self) -> None:
        """Driver exceptions should return a JSON error, not crash."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_session.run.side_effect = Exception("Connection timeout")

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            result_str = kg_mod.update_person_names(
                name="David Stewart",
                first_name="David",
                last_name="Stewart",
                agent_id="ada",
            )
            result = json.loads(result_str)

        assert "error" in result
        assert "Connection timeout" in result["error"]


class TestKGToolsRegistration:
    """Tests that new functions are registered in KG_TOOLS."""

    def test_audit_person_nodes_in_kg_tools(self) -> None:
        """audit_person_nodes should be in KG_TOOLS for MCP auto-registration."""
        from tools.stateful.knowledge_graph import KG_TOOLS, audit_person_nodes

        assert audit_person_nodes in KG_TOOLS

    def test_update_person_names_in_kg_tools(self) -> None:
        """update_person_names should be in KG_TOOLS for MCP auto-registration."""
        from tools.stateful.knowledge_graph import KG_TOOLS, update_person_names

        assert update_person_names in KG_TOOLS
