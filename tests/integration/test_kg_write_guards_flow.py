"""Integration tests for the full disambiguation and orphan guard flow.

Tests exercise the complete call chain: graph_upsert -> core.kg_guards -> Neo4j mock.
"""

import json
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_neo4j_and_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure agents.knowledge_graph can be imported by mocking neo4j and agent_tooling."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)


def _make_mock_driver() -> MagicMock:
    """Create a mock Neo4j driver."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    mock_result = MagicMock()
    mock_result.single.return_value = {"id": "test-id"}
    mock_session.run.return_value = mock_result
    return mock_driver


def _make_mock_driver_with_disambig_results(rows: list[dict]) -> MagicMock:
    """Create a mock driver that returns given rows for disambiguation query,
    then normal results for subsequent calls."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    # First call (from check_disambiguation) returns the disambig results
    disambig_result = MagicMock()
    disambig_result.data.return_value = rows

    # Subsequent calls (from graph_upsert_direct) return normal results
    upsert_result = MagicMock()
    upsert_result.single.return_value = {"id": "test-id"}

    mock_session.run.side_effect = [disambig_result, upsert_result, upsert_result]

    return mock_driver


class TestDisambiguationBlocksWriteFlow:
    """Full flow: graph_upsert called with a name that has a similar existing node."""

    def test_disambiguation_blocks_write_and_sends_telegram(self) -> None:
        """When similar node exists, write should be rejected and Telegram message sent."""
        # The disambiguation driver will return a similar node
        disambig_driver = MagicMock()
        disambig_session = MagicMock()
        disambig_driver.session.return_value.__enter__ = MagicMock(return_value=disambig_session)
        disambig_driver.session.return_value.__exit__ = MagicMock(return_value=False)

        disambig_result = MagicMock()
        disambig_result.data.return_value = [
            {"name": "Daniel Stewart", "labels": ["Person"], "id": "node-1"}
        ]
        disambig_session.run.return_value = disambig_result

        import tools.stateful.knowledge_graph as kg_mod
        from core import kg_guards

        with (
            patch.object(kg_guards, "_get_driver", return_value=disambig_driver),
            patch.object(kg_guards, "_telegram_direct", return_value=(True, "sent")) as mock_tg,
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
        assert len(result["similar_nodes"]) == 1
        mock_tg.assert_called_once()
        call_msg = mock_tg.call_args[0][0]
        assert "Daniel" in call_msg
        assert "Daniel Stewart" in call_msg


class TestOrphanGuardBlocksGraphUpsert:
    """Full flow: graph_upsert called without relation/target."""

    def test_orphan_guard_blocks_graph_upsert_without_edges(self) -> None:
        """Write should be rejected with correct error message when no edges."""
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


class TestGracePeriodAllowsOrphanViaDirect:
    """graph_upsert_direct without relation succeeds (epilogue use), and created_at is set."""

    def test_grace_period_allows_temporary_orphan_via_direct(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.knowledge_graph as kg_mod

        before = time.time()

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

        after = time.time()

        assert result["upserted"] is True

        # Verify created_at timestamp is set
        mock_session = mock_driver.session.return_value.__enter__.return_value
        call_args = mock_session.run.call_args_list[0]
        props = call_args[1]["props"]
        assert "created_at" in props
        assert before <= props["created_at"] <= after
