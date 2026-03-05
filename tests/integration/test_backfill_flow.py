"""Integration tests for the full backfill flow -- scan, classify, assign, review."""

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
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver


class TestFullBackfillClassifiesAllMemories:
    """End-to-end test: scan returns unclassified memories, classifier assigns some."""

    def test_full_backfill_classifies_all_memories(self) -> None:
        from agents.memory_backfill import BackfillEntry, memory_backfill

        mock_driver = _make_mock_driver()
        entries = [
            BackfillEntry("4:a:0", "Daniel's wife Xiaolan", "durable,person", 1000, "user", "memory", None),
            BackfillEntry("4:a:1", "Session 48ec54d4 recovered", "session", 2000, "user", "memory", None),
            BackfillEntry("4:a:2", "Daniel prefers dark mode and likes Vim", "preference", 3000, "user", "memory", None),
            BackfillEntry("4:a:3", "Something vague", "", None, "user", "memory", None),
            BackfillEntry("4:a:4", "Random content", "", None, "user", "memory", None),
        ]

        with (
            patch("agents.memory_backfill._get_driver", return_value=mock_driver),
            patch("agents.memory_backfill._scan_unclassified_memories", return_value=entries),
            patch("agents.memory_backfill._scan_unclassified_entities", return_value=[]),
            patch("agents.memory_backfill._send_review_batches") as mock_send,
        ):
            result_str = memory_backfill()
            result = json.loads(result_str)

            assert result["total_scanned"] == 5
            # First 3 should be high-confidence (tags match), last 2 should be review
            assert result["auto_assigned"] == 3
            assert result["needs_review"] == 2
            # Review batches should be sent for low-confidence entries
            mock_send.assert_called_once()
            review_entries = mock_send.call_args[0][0]
            assert len(review_entries) == 2


class TestFullBackfillClassifiesEntities:
    """End-to-end test: entity nodes classified based on entity type."""

    def test_full_backfill_classifies_entities(self) -> None:
        from agents.memory_backfill import BackfillEntry, memory_backfill

        mock_driver = _make_mock_driver()
        entity_entries = [
            BackfillEntry("4:b:0", "Daniel", "", None, "user", "entity", "Person"),
            BackfillEntry("4:b:1", "Dark Mode", "", None, "user", "entity", "Preference"),
            BackfillEntry("4:b:2", "Hive Mind", "", None, "user", "entity", "Project"),
        ]

        with (
            patch("agents.memory_backfill._get_driver", return_value=mock_driver),
            patch("agents.memory_backfill._scan_unclassified_memories", return_value=[]),
            patch("agents.memory_backfill._scan_unclassified_entities", return_value=entity_entries),
            patch("agents.memory_backfill._send_review_batches"),
        ):
            result_str = memory_backfill()
            result = json.loads(result_str)

            assert result["total_scanned"] == 3
            # All entity types should map with high confidence
            assert result["auto_assigned"] == 3
            assert result["needs_review"] == 0


class TestBackfillThenStatusShowsComplete:
    """After running backfill + applying all classifications, status shows 0 unclassified."""

    def test_backfill_then_status_shows_complete(self) -> None:
        from agents.memory_backfill import memory_backfill_status

        # Simulate all entries classified (no NULL data_class)
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        memory_counts = [
            {"class": "person", "count": 5},
            {"class": "preference", "count": 3},
            {"class": "session-log", "count": 10},
            {"class": "technical-config", "count": 2},
        ]
        entity_counts = []

        def _make_records(counts):
            records = []
            for rec in counts:
                mock_record = MagicMock()
                mock_record.__getitem__ = lambda self, key, r=rec: r[key]
                records.append(mock_record)
            mock_result = MagicMock()
            mock_result.__iter__ = MagicMock(return_value=iter(records))
            return mock_result

        mock_session.run.side_effect = [
            _make_records(memory_counts),
            _make_records(entity_counts),
        ]

        with patch("agents.memory_backfill._get_driver", return_value=mock_driver):
            result_str = memory_backfill_status()
            result = json.loads(result_str)

            assert result["complete"] is True
            assert result["unclassified"] == 0
            assert result["total"] == 20


class TestEpilogueWriteAfterRequiredDataClass:
    """After data_class is made required, epilogue write_to_memory works correctly."""

    def test_epilogue_write_after_required_data_class(self) -> None:
        import asyncio

        calls: list[dict] = []

        def capture_mem(**kwargs):
            calls.append({"type": "memory", **kwargs})
            return json.dumps({"stored": True, "id": "test", "data_class": kwargs.get("data_class")})

        def capture_kg(**kwargs):
            calls.append({"type": "graph", **kwargs})
            return json.dumps({"upserted": True, "id": "test"})

        digest = {
            "topics": ["Session covered deployment architecture."],
            "entities": [
                {"name": "Daniel", "type": "person", "context": "Owner"},
            ],
            "relationships": [],
        }

        with (
            patch("agents.memory.memory_store_direct", side_effect=capture_mem),
            patch("agents.knowledge_graph.graph_upsert_direct", side_effect=capture_kg),
        ):
            from core.epilogue import write_to_memory
            asyncio.run(write_to_memory(digest))

        # All calls should have data_class set (not None)
        for call in calls:
            assert call.get("data_class") is not None, f"Call missing data_class: {call}"
