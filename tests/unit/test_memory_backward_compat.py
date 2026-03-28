"""Unit tests for data_class requirement -- calling without data_class raises TypeError."""

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
    """Create a mock Neo4j driver."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    mock_result = MagicMock()
    mock_result.single.return_value = {"id": "test-id"}
    mock_session.run.return_value = mock_result
    return mock_driver


class TestMemoryStoreRequiresDataClass:
    """Tests that memory_store_direct requires data_class (no longer backward compat)."""

    def test_memory_store_direct_no_data_class_raises_type_error(self) -> None:
        import tools.stateful.memory as mem_mod

        with pytest.raises(TypeError):
            mem_mod.memory_store_direct(
                content="A memory entry without data_class",
                tags="test",
                source="user",
            )

    def test_memory_store_direct_with_data_class_stores_successfully(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="A memory entry with data_class",
                tags="test",
                source="user",
                data_class="technical-config",
            )
            result = json.loads(result_str)
            assert result["stored"] is True


class TestGraphUpsertRequiresDataClass:
    """Tests that graph_upsert_direct requires data_class (no longer backward compat)."""

    def test_graph_upsert_direct_no_data_class_raises_type_error(self) -> None:
        import tools.stateful.knowledge_graph as kg_mod

        with pytest.raises(TypeError):
            kg_mod.graph_upsert_direct(
                entity_type="Person",
                name="Daniel",
                agent_id="ada",
            )

    def test_graph_upsert_direct_with_data_class_upserts_successfully(self) -> None:
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
            )
            result = json.loads(result_str)
            assert result["upserted"] is True


class TestMemoryRetrieveBackwardCompat:
    """Tests that memory_retrieve handles entries with and without metadata."""

    def test_memory_retrieve_returns_entries_with_and_without_metadata(self) -> None:
        import tools.stateful.memory as mem_mod

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
            "codebase_ref": None,
            "archived": None,
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
            "codebase_ref": None,
            "archived": None,
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
            # Second entry has None metadata (legacy data in DB)
            assert memories[1]["data_class"] is None
