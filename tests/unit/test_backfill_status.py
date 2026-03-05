"""Unit tests for memory_backfill_status MCP tool."""

import json
import sys
from unittest.mock import MagicMock, patch, call

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


def _make_mock_records(counts: list[dict]) -> MagicMock:
    """Create a mock result iterable from a list of record dicts."""
    mock_records = []
    for rec in counts:
        mock_record = MagicMock()
        mock_record.__getitem__ = lambda self, key, r=rec: r[key]
        mock_record.data.return_value = rec
        mock_records.append(mock_record)
    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(return_value=iter(mock_records))
    return mock_result


def _make_mock_driver_with_counts(
    memory_counts: list[dict],
    entity_counts: list[dict] | None = None,
) -> MagicMock:
    """Create a mock driver returning class distribution counts for Memory and entity nodes."""
    if entity_counts is None:
        entity_counts = []

    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    # session.run() is called twice: once for Memory, once for entities
    mock_session.run.side_effect = [
        _make_mock_records(memory_counts),
        _make_mock_records(entity_counts),
    ]

    return mock_driver


class TestBackfillStatus:
    """Tests for the memory_backfill_status tool."""

    def test_backfill_status_all_classified(self) -> None:
        from agents.memory_backfill import memory_backfill_status

        memory_counts = [
            {"class": "person", "count": 5},
            {"class": "preference", "count": 3},
        ]
        entity_counts = [
            {"class": "person", "count": 2},
        ]
        mock_driver = _make_mock_driver_with_counts(memory_counts, entity_counts)

        with patch("agents.memory_backfill._get_driver", return_value=mock_driver):
            result_str = memory_backfill_status()
            result = json.loads(result_str)
            assert result["complete"] is True
            assert result["unclassified"] == 0

    def test_backfill_status_some_unclassified(self) -> None:
        from agents.memory_backfill import memory_backfill_status

        memory_counts = [
            {"class": "person", "count": 5},
            {"class": None, "count": 3},
        ]
        entity_counts = [
            {"class": None, "count": 2},
        ]
        mock_driver = _make_mock_driver_with_counts(memory_counts, entity_counts)

        with patch("agents.memory_backfill._get_driver", return_value=mock_driver):
            result_str = memory_backfill_status()
            result = json.loads(result_str)
            assert result["complete"] is False
            assert result["unclassified"] == 5  # 3 memory + 2 entity
            assert result["memory_unclassified"] == 3
            assert result["entity_unclassified"] == 2

    def test_backfill_status_returns_class_distribution(self) -> None:
        from agents.memory_backfill import memory_backfill_status

        memory_counts = [
            {"class": "person", "count": 5},
            {"class": "preference", "count": 3},
            {"class": "session-log", "count": 10},
        ]
        entity_counts = [
            {"class": "person", "count": 2},
            {"class": "technical-config", "count": 1},
        ]
        mock_driver = _make_mock_driver_with_counts(memory_counts, entity_counts)

        with patch("agents.memory_backfill._get_driver", return_value=mock_driver):
            result_str = memory_backfill_status()
            result = json.loads(result_str)
            dist = result["distribution"]
            assert dist["person"] == 5
            assert dist["preference"] == 3
            assert dist["session-log"] == 10
            entity_dist = result["entity_distribution"]
            assert entity_dist["person"] == 2
            assert entity_dist["technical-config"] == 1

    def test_backfill_status_includes_entity_nodes(self) -> None:
        """Verify that entity nodes with NULL data_class are counted as unclassified."""
        from agents.memory_backfill import memory_backfill_status

        memory_counts = [
            {"class": "person", "count": 5},
        ]
        entity_counts = [
            {"class": None, "count": 4},
        ]
        mock_driver = _make_mock_driver_with_counts(memory_counts, entity_counts)

        with patch("agents.memory_backfill._get_driver", return_value=mock_driver):
            result_str = memory_backfill_status()
            result = json.loads(result_str)
            assert result["complete"] is False
            assert result["entity_unclassified"] == 4
            assert result["unclassified"] == 4
