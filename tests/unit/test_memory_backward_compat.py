"""Unit tests for backward compatibility — calling without data_class still works."""

import json
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_neo4j_and_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure agents.knowledge_graph can be imported by mocking neo4j and agent_tooling."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


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


class TestMemoryStoreBackwardCompat:
    """Tests that memory_store_direct works without data_class (backward compat)."""

    def test_memory_store_direct_no_data_class_still_stores(self) -> None:
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="A backward-compatible memory entry",
                tags="test",
                source="user",
            )
            result = json.loads(result_str)
            assert result["stored"] is True

    def test_memory_store_direct_no_data_class_logs_deprecation_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_driver = _make_mock_driver()
        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
            caplog.at_level(logging.WARNING),
        ):
            mem_mod.memory_store_direct(
                content="No data_class provided",
                source="user",
            )
            assert any("deprecat" in msg.lower() for msg in caplog.messages)


class TestGraphUpsertBackwardCompat:
    """Tests that graph_upsert_direct works without data_class (backward compat)."""

    def test_graph_upsert_direct_no_data_class_still_upserts(self) -> None:
        mock_driver = _make_mock_driver()
        import agents.knowledge_graph as kg_mod

        with (
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
        ):
            result_str = kg_mod.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
            )
            result = json.loads(result_str)
            assert result["upserted"] is True

    def test_graph_upsert_direct_no_data_class_logs_deprecation_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_driver = _make_mock_driver()
        import agents.knowledge_graph as kg_mod

        with (
            patch.object(kg_mod, "_get_driver", return_value=mock_driver),
            patch.object(kg_mod, "_kg_index_created", True),
            caplog.at_level(logging.WARNING),
        ):
            kg_mod.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
            )
            assert any("deprecat" in msg.lower() for msg in caplog.messages)


class TestMemoryRetrieveBackwardCompat:
    """Tests that memory_retrieve handles entries with and without metadata."""

    def test_memory_retrieve_returns_entries_with_and_without_metadata(self) -> None:
        import agents.memory as mem_mod

        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        # Mock Neo4j to return mixed entries
        record_with_meta = MagicMock()
        record_with_meta.__getitem__ = lambda self, key: {
            "content": "Entry with metadata",
            "tags": "test",
            "source": "user",
            "agent_id": "ada",
            "created_at": 1000,
            "score": 0.95,
            "data_class": "person",
            "tier": "durable",
            "as_of": "2026-01-01T00:00:00Z",
            "expires_at": None,
            "superseded": False,
        }[key]

        record_without_meta = MagicMock()
        record_without_meta.__getitem__ = lambda self, key: {
            "content": "Legacy entry without metadata",
            "tags": "legacy",
            "source": "user",
            "agent_id": "ada",
            "created_at": 999,
            "score": 0.85,
            "data_class": None,
            "tier": None,
            "as_of": None,
            "expires_at": None,
            "superseded": None,
        }[key]

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(
            return_value=iter([record_with_meta, record_without_meta])
        )
        mock_session.run.return_value = mock_result

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_retrieve(query="test query")
            result = json.loads(result_str)
            assert result["count"] == 2
            memories = result["memories"]
            # First entry has metadata
            assert memories[0]["data_class"] == "person"
            assert memories[0]["tier"] == "durable"
            # Second entry has None metadata (backward compat)
            assert memories[1]["data_class"] is None
