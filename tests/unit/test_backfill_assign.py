"""Unit tests for backfill auto-assignment — applying classifications to Neo4j nodes."""

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from core.backfill_classifier import ClassificationResult


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


class TestAssignClassification:
    """Tests for _assign_classification function."""

    def test_assign_memory_updates_neo4j_node(self) -> None:
        from agents.memory_backfill import BackfillEntry, _assign_classification

        entry = BackfillEntry(
            element_id="4:abc:0",
            content="Daniel's preference",
            tags="preference",
            created_at=1000,
            source="user",
            node_type="memory",
            entity_type=None,
        )
        result = ClassificationResult(
            data_class="preference",
            confidence=0.9,
            reason="tag match",
            candidates=["preference"],
        )
        mock_driver = _make_mock_driver()
        success = _assign_classification(mock_driver, entry, result)
        assert success is True

        # Verify Cypher SET was called
        mock_session = mock_driver.session.return_value.__enter__.return_value
        assert mock_session.run.called

    def test_assign_entity_updates_neo4j_node(self) -> None:
        from agents.memory_backfill import BackfillEntry, _assign_classification

        entry = BackfillEntry(
            element_id="4:def:0",
            content="Daniel",
            tags="",
            created_at=None,
            source="user",
            node_type="entity",
            entity_type="Person",
        )
        result = ClassificationResult(
            data_class="person",
            confidence=0.85,
            reason="entity type match",
            candidates=["person"],
        )
        mock_driver = _make_mock_driver()
        success = _assign_classification(mock_driver, entry, result)
        assert success is True

    def test_assign_uses_created_at_for_as_of(self) -> None:
        from agents.memory_backfill import BackfillEntry, _assign_classification

        entry = BackfillEntry(
            element_id="4:abc:1",
            content="Some content",
            tags="",
            created_at=1709251200,  # 2024-03-01
            source="user",
            node_type="memory",
            entity_type=None,
        )
        result = ClassificationResult(
            data_class="person",
            confidence=0.9,
            reason="test",
            candidates=["person"],
        )
        mock_driver = _make_mock_driver()
        _assign_classification(mock_driver, entry, result)

        mock_session = mock_driver.session.return_value.__enter__.return_value
        call_args = mock_session.run.call_args
        params = call_args[1]
        # as_of should be derived from created_at
        assert "2024" in params["as_of"] or "1709" in str(params["as_of"])

    def test_assign_defaults_as_of_to_now_when_no_created_at(self) -> None:
        from agents.memory_backfill import BackfillEntry, _assign_classification

        entry = BackfillEntry(
            element_id="4:abc:2",
            content="Some content",
            tags="",
            created_at=None,
            source="user",
            node_type="memory",
            entity_type=None,
        )
        result = ClassificationResult(
            data_class="person",
            confidence=0.9,
            reason="test",
            candidates=["person"],
        )
        mock_driver = _make_mock_driver()
        before = datetime.now(timezone.utc).isoformat()
        _assign_classification(mock_driver, entry, result)
        after = datetime.now(timezone.utc).isoformat()

        mock_session = mock_driver.session.return_value.__enter__.return_value
        call_args = mock_session.run.call_args
        params = call_args[1]
        assert before <= params["as_of"] <= after

    def test_assign_skips_low_confidence(self) -> None:
        from agents.memory_backfill import BackfillEntry, _assign_classification

        entry = BackfillEntry(
            element_id="4:abc:3",
            content="Ambiguous",
            tags="",
            created_at=None,
            source="user",
            node_type="memory",
            entity_type=None,
        )
        result = ClassificationResult(
            data_class="person",
            confidence=0.4,
            reason="low confidence",
            candidates=["person", "preference"],
        )
        mock_driver = _make_mock_driver()
        success = _assign_classification(mock_driver, entry, result)
        assert success is False

        # Cypher should NOT have been called
        mock_session = mock_driver.session.return_value.__enter__.return_value
        assert not mock_session.run.called


class TestAutoAssignBatch:
    """Tests for _auto_assign_batch function."""

    def test_assign_returns_counts(self) -> None:
        from agents.memory_backfill import BackfillEntry, _auto_assign_batch

        entries = [
            BackfillEntry("4:a:0", "Person data", "durable,person", 1000, "user", "memory", None),
            BackfillEntry("4:a:1", "Something vague", "", None, "user", "memory", None),
            BackfillEntry("4:a:2", "Scheduled meeting at 3pm", "event", 2000, "user", "memory", None),
        ]
        mock_driver = _make_mock_driver()
        counts, low_conf = _auto_assign_batch(mock_driver, entries)
        assert "assigned" in counts
        assert "skipped" in counts
        assert counts["assigned"] + counts["skipped"] == len(entries)
        # Low confidence entries should be returned for review
        assert isinstance(low_conf, list)

    def test_assign_preserves_existing_source(self) -> None:
        from agents.memory_backfill import BackfillEntry, _assign_classification

        entry = BackfillEntry(
            element_id="4:abc:4",
            content="Test data",
            tags="person",
            created_at=1000,
            source="tool",
            node_type="memory",
            entity_type=None,
        )
        result = ClassificationResult(
            data_class="person",
            confidence=0.9,
            reason="test",
            candidates=["person"],
        )
        mock_driver = _make_mock_driver()
        _assign_classification(mock_driver, entry, result)

        # The Cypher query should not overwrite source
        mock_session = mock_driver.session.return_value.__enter__.return_value
        call_args = mock_session.run.call_args
        query = call_args[0][0]
        # Source should NOT be in the SET clause
        assert "source" not in query.lower() or "n.source" not in query
