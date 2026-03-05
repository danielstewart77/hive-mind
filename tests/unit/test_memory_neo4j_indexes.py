"""Unit tests for Neo4j metadata index creation in memory.py and knowledge_graph.py."""

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


class TestMemoryEnsureIndex:
    """Tests for _ensure_index in agents/memory.py."""

    def test_ensure_index_creates_vector_index(self) -> None:
        import agents.memory as mem_mod

        # Reset the global guard
        with patch.object(mem_mod, "_index_created", False):
            mock_session = MagicMock()
            mem_mod._ensure_index(mock_session)

            # Should have called run multiple times: vector + metadata indexes
            calls = mock_session.run.call_args_list
            assert len(calls) >= 1
            # First call should be the vector index
            first_query = calls[0][0][0]
            assert "VECTOR INDEX" in first_query.upper() or "vector" in first_query.lower()

    def test_ensure_index_creates_tier_index(self) -> None:
        import agents.memory as mem_mod

        with patch.object(mem_mod, "_index_created", False):
            mock_session = MagicMock()
            mem_mod._ensure_index(mock_session)

            all_queries = [c[0][0] for c in mock_session.run.call_args_list]
            tier_queries = [q for q in all_queries if "tier" in q.lower()]
            assert len(tier_queries) >= 1

    def test_ensure_index_creates_data_class_index(self) -> None:
        import agents.memory as mem_mod

        with patch.object(mem_mod, "_index_created", False):
            mock_session = MagicMock()
            mem_mod._ensure_index(mock_session)

            all_queries = [c[0][0] for c in mock_session.run.call_args_list]
            dc_queries = [q for q in all_queries if "data_class" in q.lower()]
            assert len(dc_queries) >= 1

    def test_ensure_index_creates_expires_at_index(self) -> None:
        import agents.memory as mem_mod

        with patch.object(mem_mod, "_index_created", False):
            mock_session = MagicMock()
            mem_mod._ensure_index(mock_session)

            all_queries = [c[0][0] for c in mock_session.run.call_args_list]
            exp_queries = [q for q in all_queries if "expires_at" in q.lower()]
            assert len(exp_queries) >= 1

    def test_ensure_index_creates_source_index(self) -> None:
        import agents.memory as mem_mod

        with patch.object(mem_mod, "_index_created", False):
            mock_session = MagicMock()
            mem_mod._ensure_index(mock_session)

            all_queries = [c[0][0] for c in mock_session.run.call_args_list]
            src_queries = [q for q in all_queries if "source" in q.lower() and "INDEX" in q.upper()]
            assert len(src_queries) >= 1

    def test_ensure_index_idempotent(self) -> None:
        import agents.memory as mem_mod

        with patch.object(mem_mod, "_index_created", False):
            mock_session = MagicMock()
            mem_mod._ensure_index(mock_session)
            first_call_count = mock_session.run.call_count

            # Second call should do nothing because _index_created is now True
            mem_mod._ensure_index(mock_session)
            assert mock_session.run.call_count == first_call_count


class TestKnowledgeGraphEnsureMetadataIndexes:
    """Tests for _ensure_metadata_indexes in agents/knowledge_graph.py."""

    def test_ensure_metadata_indexes_creates_indexes(self) -> None:
        import agents.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_kg_index_created", False):
            mock_session = MagicMock()
            kg_mod._ensure_metadata_indexes(mock_session)

            # Should create indexes for all entity types and metadata fields
            calls = mock_session.run.call_args_list
            # 5 entity types * 3 fields = 15 indexes
            assert len(calls) >= 15

    def test_ensure_metadata_indexes_idempotent(self) -> None:
        import agents.knowledge_graph as kg_mod

        with patch.object(kg_mod, "_kg_index_created", False):
            mock_session = MagicMock()
            kg_mod._ensure_metadata_indexes(mock_session)
            first_count = mock_session.run.call_count

            # Second call should do nothing
            kg_mod._ensure_metadata_indexes(mock_session)
            assert mock_session.run.call_count == first_count
