"""Integration tests for the audit-to-update person node flow.

Tests exercise the full call chain: audit_person_nodes -> update_person_names -> search_person.
"""

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
    return mock_driver


class TestAuditThenUpdateFlow:
    """End-to-end flow: audit finds nodes, then update sets names."""

    def test_audit_finds_nodes_then_update_sets_names(self) -> None:
        """audit_person_nodes returns incomplete nodes; update_person_names patches them."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        # audit_person_nodes query result
        audit_result = MagicMock()
        audit_result.data.return_value = [
            {
                "n": {"name": "David Stewart", "first_name": None, "last_name": None, "agent_id": "ada"},
                "element_id": "4:abc:123",
            },
            {
                "n": {"name": "Jane Smith", "first_name": None, "last_name": None, "agent_id": "ada"},
                "element_id": "4:abc:456",
            },
        ]

        # update_person_names result (node found)
        update_result = MagicMock()
        update_result.single.return_value = {"name": "David Stewart"}

        mock_session.run.side_effect = [audit_result, update_result]

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            # Step 1: Audit
            audit_str = kg_mod.audit_person_nodes(agent_id="ada")
            audit = json.loads(audit_str)

            assert audit["found"] is True
            assert audit["count"] == 2

            # Step 2: Update the first node
            update_str = kg_mod.update_person_names(
                name="David Stewart",
                first_name="David",
                last_name="Stewart",
                agent_id="ada",
            )
            update = json.loads(update_str)

            assert update["updated"] is True
            assert update["first_name"] == "David"
            assert update["last_name"] == "Stewart"

        # Verify the update Cypher included correct params
        update_call = mock_session.run.call_args_list[1]
        params = update_call[1]
        assert params["first_name"] == "David"
        assert params["last_name"] == "Stewart"
        assert params["name"] == "David Stewart"
        assert params["agent_id"] == "ada"

    def test_updated_node_discoverable_by_search_person(self) -> None:
        """After update_person_names, search_person should find the node by first/last name."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        # update_person_names result
        update_result = MagicMock()
        update_result.single.return_value = {"name": "David Stewart"}

        # search_person result (node now has first_name/last_name)
        search_result = MagicMock()
        search_result.data.return_value = [
            {
                "n": {
                    "name": "David Stewart",
                    "first_name": "David",
                    "last_name": "Stewart",
                    "agent_id": "ada",
                }
            }
        ]

        mock_session.run.side_effect = [update_result, search_result]

        import tools.stateful.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_get_driver", return_value=mock_driver):
            # Update the node
            update_str = kg_mod.update_person_names(
                name="David Stewart",
                first_name="David",
                last_name="Stewart",
                agent_id="ada",
            )
            update = json.loads(update_str)
            assert update["updated"] is True

            # Now search should find it
            search_str = kg_mod.search_person(
                first_name="David",
                last_name="Stewart",
                agent_id="ada",
            )
            search = json.loads(search_str)

        assert search["found"] is True
        assert search["count"] == 1
        assert search["matches"][0]["first_name"] == "David"
        assert search["matches"][0]["last_name"] == "Stewart"
