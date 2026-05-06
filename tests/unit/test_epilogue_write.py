"""Unit tests for auto_write_digest function."""

from unittest.mock import patch

from core.epilogue import (
    EpilogueDigest,
    SessionMetrics,
    auto_write_digest,
)


def _make_digest(
    memories: list | None = None,
    entities: list | None = None,
) -> EpilogueDigest:
    return EpilogueDigest(
        session_id="test-session-id",
        summary="Test summary",
        memories=memories or [],
        entities=entities or [],
        metrics=SessionMetrics(turn_count=5, duration_minutes=15.0, novel_entity_count=1),
    )


class TestAutoWriteDigest:
    """Tests for auto_write_digest() function."""

    @patch("core.epilogue._graph_upsert_direct")
    @patch("core.epilogue._memory_store_direct")
    def test_calls_memory_store_for_each_memory(self, mock_mem, mock_graph) -> None:
        mock_mem.return_value = {"id": 1}
        digest = _make_digest(memories=[
            {"content": "M1", "data_class": "observation", "tags": "t1", "source": "user"},
            {"content": "M2", "data_class": "observation", "tags": "t2", "source": "user"},
            {"content": "M3", "data_class": "session-summary", "tags": "", "source": "self"},
        ])
        auto_write_digest(digest)
        assert mock_mem.call_count == 3

    @patch("core.epilogue._graph_upsert_direct")
    @patch("core.epilogue._memory_store_direct")
    def test_calls_graph_upsert_for_each_entity(self, mock_mem, mock_graph) -> None:
        mock_graph.return_value = {"upserted": True, "id": 1}
        digest = _make_digest(entities=[
            {"entity_type": "Person", "name": "Alice", "data_class": "contact", "properties": "{}", "agent_id": "ada"},
            {"entity_type": "Project", "name": "Hive Mind", "data_class": "project", "properties": "{}", "agent_id": "ada"},
        ])
        auto_write_digest(digest)
        assert mock_graph.call_count == 2

    @patch("core.epilogue._graph_upsert_direct")
    @patch("core.epilogue._memory_store_direct")
    def test_returns_counts(self, mock_mem, mock_graph) -> None:
        mock_mem.return_value = {"id": 1}
        mock_graph.return_value = {"upserted": True, "id": 1}
        digest = _make_digest(
            memories=[{"content": "M1", "data_class": "obs", "tags": "", "source": "user"}],
            entities=[{"entity_type": "Person", "name": "A", "data_class": "c", "properties": "{}", "agent_id": "ada"}],
        )
        result = auto_write_digest(digest)
        assert result["memories_written"] == 1
        assert result["entities_written"] == 1

    @patch("core.epilogue._graph_upsert_direct")
    @patch("core.epilogue._memory_store_direct")
    def test_handles_partial_failure(self, mock_mem, mock_graph) -> None:
        mock_mem.side_effect = [
            {"id": 1},
            {"stored": False, "error": "write failed"},
            {"id": 2},
        ]
        digest = _make_digest(memories=[
            {"content": "M1", "data_class": "obs", "tags": "", "source": "user"},
            {"content": "M2", "data_class": "obs", "tags": "", "source": "user"},
            {"content": "M3", "data_class": "obs", "tags": "", "source": "user"},
        ])
        result = auto_write_digest(digest)
        assert result["memories_written"] == 2
        assert result["errors"] == 1

    @patch("core.epilogue._graph_upsert_direct")
    @patch("core.epilogue._memory_store_direct")
    def test_empty_digest_returns_zero_counts(self, mock_mem, mock_graph) -> None:
        digest = _make_digest(memories=[], entities=[])
        result = auto_write_digest(digest)
        assert result["memories_written"] == 0
        assert result["entities_written"] == 0
        assert result["errors"] == 0
