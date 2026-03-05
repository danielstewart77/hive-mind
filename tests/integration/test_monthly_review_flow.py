"""Integration tests for the monthly review flow.

Tests the full flow from query to handler execution,
composing the sweep query, handler functions, and archive store
in realistic combinations.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j and agent_tooling for importing agents.memory / core modules."""
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


def _make_mock_driver_with_query_results(records: list[dict]) -> MagicMock:
    """Create a mock Neo4j driver that returns given records from the query."""
    mock_driver = _make_mock_driver()
    mock_session = mock_driver.session.return_value.__enter__.return_value

    mock_query_result = MagicMock()
    mock_query_result.__iter__ = MagicMock(return_value=iter(records))
    mock_session.run.return_value = mock_query_result

    return mock_driver


class TestFullReviewFlowKeep:
    """Integration test: keep handler updates last_reviewed_at."""

    def test_full_review_flow_keep_updates_last_reviewed_at(self) -> None:
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        mock_result = MagicMock()
        mock_result.single.return_value = {"count": 1}
        mock_session.run.return_value = mock_result

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.handle_keep("4:abc:123")

        assert result["ok"] is True
        assert result["action"] == "keep"

        # Verify the SET query was called with last_reviewed_at
        cypher = mock_session.run.call_args[0][0]
        assert "last_reviewed_at" in cypher
        assert "SET" in cypher
        # Verify timestamp was passed
        params = mock_session.run.call_args[1]
        assert "now" in params
        assert isinstance(params["now"], int)


class TestFullReviewFlowArchive:
    """Integration test: archive handler saves to store AND marks archived in Neo4j."""

    def test_full_review_flow_archive_saves_and_marks(self, tmp_path: Path) -> None:
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        # First call: entry lookup
        mock_entry = MagicMock()
        mock_entry.single.return_value = {
            "content": "Earthquake in Turkey",
            "data_class": "world-event",
            "tags": "news",
            "source": "user",
            "agent_id": "ada",
            "created_at": 1700000000,
            "props": {"tier": "T3", "data_class": "world-event"},
        }
        # Second call: SET archived
        mock_set_result = MagicMock()
        mock_set_result.single.return_value = {"count": 1}
        mock_session.run.side_effect = [mock_entry, mock_set_result]

        from core import monthly_review
        from core.archive_store import ArchiveStore

        store = ArchiveStore(tmp_path / "archive.json")

        with (
            patch.object(monthly_review, "_get_driver", return_value=mock_driver),
            patch.object(monthly_review, "_get_archive_store", return_value=store),
        ):
            result = monthly_review.handle_archive("4:abc:123")

        assert result["ok"] is True
        assert result["action"] == "archive"

        # Verify entry was saved to archive store
        entries = store.list_all()
        assert len(entries) == 1
        assert entries[0].content == "Earthquake in Turkey"
        assert entries[0].original_id == "4:abc:123"

        # Verify SET archived query was called
        set_call = mock_session.run.call_args_list[-1]
        cypher = set_call[0][0]
        assert "archived" in cypher.lower()
        assert "true" in cypher.lower()


class TestFullReviewFlowDiscard:
    """Integration test: discard handler runs DETACH DELETE."""

    def test_full_review_flow_discard_deletes_entry(self) -> None:
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.handle_discard("4:abc:123")

        assert result["ok"] is True
        assert result["action"] == "discard"

        cypher = mock_session.run.call_args[0][0]
        assert "DETACH DELETE" in cypher
        assert mock_session.run.call_args[1]["id"] == "4:abc:123"


class TestMonthlyReviewSweepToTelegram:
    """Integration test: sweep queries entries, builds messages, sends via Telegram."""

    def test_monthly_review_sweep_to_telegram_message(self) -> None:
        records = [
            {
                "content": "Earthquake in Turkey",
                "data_class": "world-event",
                "created_at": 1700000000,
                "last_reviewed_at": None,
                "id": "4:abc:123",
            },
            {
                "content": "Learn Rust",
                "data_class": "intention",
                "created_at": 1700000001,
                "last_reviewed_at": None,
                "id": "4:def:456",
            },
        ]
        mock_driver = _make_mock_driver_with_query_results(records)

        from core import monthly_review

        with (
            patch.object(monthly_review, "_get_driver", return_value=mock_driver),
            patch.object(monthly_review, "_telegram_direct", return_value=(True, "sent")) as mock_tg,
        ):
            result = monthly_review.sweep_monthly_review()

        assert result["entries_found"] == 2
        assert result["messages_sent"] == 2  # one per class
        assert result["errors"] == 0

        # Verify two Telegram messages were sent (one per class)
        assert mock_tg.call_count == 2

        # Verify message content
        all_messages = [call[0][0] for call in mock_tg.call_args_list]
        combined = "\n".join(all_messages)
        assert "Earthquake in Turkey" in combined
        assert "Learn Rust" in combined
        assert "/keep_" in combined
        assert "/discard_" in combined


class TestArchivedEntryExcludedFromRetrieve:
    """Integration test: after archiving, entry no longer appears in default memory_retrieve."""

    def test_archived_entry_excluded_from_memory_retrieve(self) -> None:
        """Verify that the memory_retrieve query includes the archived filter."""
        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.run.return_value = mock_result

        import agents.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            mem_mod.memory_retrieve("earthquake")

        cypher = mock_session.run.call_args[0][0]
        # The default query should exclude archived entries
        assert "m.archived IS NULL OR m.archived = false" in cypher
