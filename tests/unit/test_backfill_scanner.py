"""Unit tests for backfill scanner — querying unclassified entries from Neo4j."""

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


def _make_mock_driver(records: list[dict] | None = None) -> MagicMock:
    """Create a mock Neo4j driver returning given records."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    if records is not None:
        mock_records = []
        for rec in records:
            mock_record = MagicMock()
            mock_record.__getitem__ = lambda self, key, r=rec: r[key]
            mock_record.data.return_value = rec
            mock_records.append(mock_record)
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter(mock_records))
        mock_session.run.return_value = mock_result
    else:
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.run.return_value = mock_result

    return mock_driver


class TestScanUnclassifiedMemories:
    """Tests for scanning Memory nodes without data_class."""

    def test_scan_unclassified_memories_returns_entries(self) -> None:
        from agents.memory_backfill import BackfillEntry, _scan_unclassified_memories

        records = [
            {
                "id": "4:abc:0",
                "content": "Test memory",
                "tags": "test",
                "created_at": 1000,
                "source": "user",
            },
        ]
        mock_driver = _make_mock_driver(records)
        entries = _scan_unclassified_memories(mock_driver)
        assert len(entries) == 1
        assert isinstance(entries[0], BackfillEntry)
        assert entries[0].content == "Test memory"
        assert entries[0].node_type == "memory"

    def test_scan_returns_empty_when_all_classified(self) -> None:
        from agents.memory_backfill import _scan_unclassified_memories

        mock_driver = _make_mock_driver([])
        entries = _scan_unclassified_memories(mock_driver)
        assert entries == []

    def test_scan_memory_entry_fields_populated(self) -> None:
        from agents.memory_backfill import _scan_unclassified_memories

        records = [
            {
                "id": "4:abc:1",
                "content": "Important fact",
                "tags": "durable,person",
                "created_at": 2000,
                "source": "session",
            },
        ]
        mock_driver = _make_mock_driver(records)
        entries = _scan_unclassified_memories(mock_driver)
        entry = entries[0]
        assert entry.element_id == "4:abc:1"
        assert entry.content == "Important fact"
        assert entry.tags == "durable,person"
        assert entry.created_at == 2000
        assert entry.source == "session"
        assert entry.node_type == "memory"


class TestScanUnclassifiedEntities:
    """Tests for scanning entity nodes without data_class."""

    def test_scan_unclassified_entities_returns_entries(self) -> None:
        from agents.memory_backfill import BackfillEntry, _scan_unclassified_entities

        records = [
            {
                "id": "4:def:0",
                "name": "Daniel",
                "labels": ["Person"],
                "properties": "{}",
            },
        ]
        mock_driver = _make_mock_driver(records)
        entries = _scan_unclassified_entities(mock_driver)
        assert len(entries) == 1
        assert entries[0].entity_type == "Person"
        assert entries[0].node_type == "entity"

    def test_scan_entity_entry_has_entity_type(self) -> None:
        from agents.memory_backfill import _scan_unclassified_entities

        records = [
            {
                "id": "4:ghi:0",
                "name": "Hive Mind",
                "labels": ["Project"],
                "properties": "{}",
            },
        ]
        mock_driver = _make_mock_driver(records)
        entries = _scan_unclassified_entities(mock_driver)
        assert entries[0].entity_type == "Project"

    def test_scan_handles_neo4j_connection_error(self) -> None:
        from agents.memory_backfill import _scan_unclassified_memories

        mock_driver = MagicMock()
        mock_driver.session.side_effect = Exception("Connection refused")
        entries = _scan_unclassified_memories(mock_driver)
        assert entries == []
