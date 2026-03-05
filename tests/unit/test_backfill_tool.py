"""Unit tests for the memory_backfill MCP tool orchestrator."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_neo4j_and_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j and agent_tooling for import."""
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
    return mock_driver


class TestMemoryBackfillTool:
    """Tests for the memory_backfill MCP tool function."""

    def test_backfill_tool_returns_summary_json(self) -> None:
        from agents.memory_backfill import BackfillEntry, memory_backfill

        mock_driver = _make_mock_driver()
        entries = [
            BackfillEntry("4:a:0", "Person data", "durable,person", 1000, "user", "memory", None),
        ]

        with (
            patch("agents.memory_backfill._get_driver", return_value=mock_driver),
            patch("agents.memory_backfill._scan_unclassified_memories", return_value=entries),
            patch("agents.memory_backfill._scan_unclassified_entities", return_value=[]),
            patch("agents.memory_backfill._send_review_batches"),
        ):
            result_str = memory_backfill()
            result = json.loads(result_str)
            assert "total_scanned" in result
            assert "auto_assigned" in result
            assert "needs_review" in result

    def test_backfill_tool_scans_both_memory_and_entities(self) -> None:
        from agents.memory_backfill import BackfillEntry, memory_backfill

        mock_driver = _make_mock_driver()
        mem_entries = [
            BackfillEntry("4:a:0", "Memory data", "session", 1000, "user", "memory", None),
        ]
        entity_entries = [
            BackfillEntry("4:b:0", "Daniel", "", None, "user", "entity", "Person"),
        ]

        with (
            patch("agents.memory_backfill._get_driver", return_value=mock_driver),
            patch("agents.memory_backfill._scan_unclassified_memories", return_value=mem_entries) as scan_mem,
            patch("agents.memory_backfill._scan_unclassified_entities", return_value=entity_entries) as scan_ent,
            patch("agents.memory_backfill._send_review_batches"),
        ):
            memory_backfill()
            scan_mem.assert_called_once()
            scan_ent.assert_called_once()

    def test_backfill_tool_auto_assigns_high_confidence(self) -> None:
        from agents.memory_backfill import BackfillEntry, memory_backfill

        mock_driver = _make_mock_driver()
        entries = [
            BackfillEntry("4:a:0", "Person data", "durable,person", 1000, "user", "memory", None),
        ]

        with (
            patch("agents.memory_backfill._get_driver", return_value=mock_driver),
            patch("agents.memory_backfill._scan_unclassified_memories", return_value=entries),
            patch("agents.memory_backfill._scan_unclassified_entities", return_value=[]),
            patch("agents.memory_backfill._send_review_batches"),
        ):
            result_str = memory_backfill()
            result = json.loads(result_str)
            assert result["auto_assigned"] >= 1

    def test_backfill_tool_sends_review_for_low_confidence(self) -> None:
        from agents.memory_backfill import BackfillEntry, memory_backfill

        mock_driver = _make_mock_driver()
        entries = [
            BackfillEntry("4:a:0", "Something vague", "", None, "user", "memory", None),
        ]

        with (
            patch("agents.memory_backfill._get_driver", return_value=mock_driver),
            patch("agents.memory_backfill._scan_unclassified_memories", return_value=entries),
            patch("agents.memory_backfill._scan_unclassified_entities", return_value=[]),
            patch("agents.memory_backfill._send_review_batches") as mock_send,
        ):
            result_str = memory_backfill()
            result = json.loads(result_str)
            assert result["needs_review"] >= 1
            mock_send.assert_called_once()

    def test_backfill_tool_handles_no_unclassified(self) -> None:
        from agents.memory_backfill import memory_backfill

        mock_driver = _make_mock_driver()

        with (
            patch("agents.memory_backfill._get_driver", return_value=mock_driver),
            patch("agents.memory_backfill._scan_unclassified_memories", return_value=[]),
            patch("agents.memory_backfill._scan_unclassified_entities", return_value=[]),
        ):
            result_str = memory_backfill()
            result = json.loads(result_str)
            assert result["total_scanned"] == 0
            assert "already classified" in result.get("message", "").lower() or result["total_scanned"] == 0

    def test_backfill_tool_handles_neo4j_unavailable(self) -> None:
        from agents.memory_backfill import memory_backfill

        with patch("agents.memory_backfill._get_driver", side_effect=Exception("Connection refused")):
            result_str = memory_backfill()
            result = json.loads(result_str)
            assert "error" in result
