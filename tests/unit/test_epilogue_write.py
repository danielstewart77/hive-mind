"""Unit tests for auto_write_digest and hitl_write_digest functions."""

import json
from unittest.mock import patch

from core.epilogue import (
    EpilogueDigest,
    SessionMetrics,
    auto_write_digest,
    hitl_write_digest,
    format_digest_for_telegram,
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
        mock_mem.return_value = json.dumps({"ok": True})
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
        mock_graph.return_value = json.dumps({"ok": True})
        digest = _make_digest(entities=[
            {"entity_type": "Person", "name": "Alice", "data_class": "contact", "properties": "{}", "agent_id": "ada"},
            {"entity_type": "Project", "name": "Hive Mind", "data_class": "project", "properties": "{}", "agent_id": "ada"},
        ])
        auto_write_digest(digest)
        assert mock_graph.call_count == 2

    @patch("core.epilogue._graph_upsert_direct")
    @patch("core.epilogue._memory_store_direct")
    def test_returns_counts(self, mock_mem, mock_graph) -> None:
        mock_mem.return_value = json.dumps({"ok": True})
        mock_graph.return_value = json.dumps({"ok": True})
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
            json.dumps({"ok": True}),
            json.dumps({"error": "write failed"}),
            json.dumps({"ok": True}),
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


class TestHitlWriteDigest:
    """Tests for hitl_write_digest() function."""

    @patch("core.epilogue.auto_write_digest")
    @patch("core.epilogue._hitl_request")
    def test_approved_writes_all(self, mock_hitl, mock_auto) -> None:
        mock_hitl.return_value = True
        mock_auto.return_value = {"memories_written": 2, "entities_written": 1, "errors": 0}
        digest = _make_digest(
            memories=[{"content": "M1", "data_class": "obs", "tags": "", "source": "user"}],
        )
        result = hitl_write_digest(digest)
        mock_auto.assert_called_once_with(digest)
        assert result["memories_written"] == 2

    @patch("core.epilogue.auto_write_digest")
    @patch("core.epilogue._hitl_request")
    def test_denied_writes_nothing(self, mock_hitl, mock_auto) -> None:
        mock_hitl.return_value = False
        digest = _make_digest(
            memories=[{"content": "M1", "data_class": "obs", "tags": "", "source": "user"}],
        )
        result = hitl_write_digest(digest)
        mock_auto.assert_not_called()
        assert result["memories_written"] == 0
        assert result["entities_written"] == 0
        assert result.get("skipped") is True

    @patch("core.epilogue.auto_write_digest")
    @patch("core.epilogue._hitl_request")
    def test_sends_formatted_digest(self, mock_hitl, mock_auto) -> None:
        mock_hitl.return_value = True
        mock_auto.return_value = {"memories_written": 0, "entities_written": 0, "errors": 0}
        digest = _make_digest(memories=[{"content": "Test memory", "data_class": "obs", "tags": "", "source": "user"}])
        expected_summary = format_digest_for_telegram(digest)
        hitl_write_digest(digest)
        mock_hitl.assert_called_once_with(expected_summary)
