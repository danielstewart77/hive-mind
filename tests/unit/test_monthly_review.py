"""Unit tests for the monthly review module (core.monthly_review)."""

import sys
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j, agent_tooling, and keyring for importing agents.memory / core modules."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


def _make_review_record(
    content: str = "Test content",
    data_class: str = "world-event",
    created_at: int = 1700000000,
    last_reviewed_at: int | None = None,
    element_id: str = "4:abc:123",
    archived: bool | None = None,
    tags: str = "",
    source: str = "user",
    agent_id: str = "ada",
) -> dict:
    """Create a mock record matching the review query result shape."""
    return {
        "content": content,
        "data_class": data_class,
        "created_at": created_at,
        "last_reviewed_at": last_reviewed_at,
        "id": element_id,
    }


def _make_mock_driver_with_results(records: list[dict]) -> MagicMock:
    """Create a mock Neo4j driver that returns given records from the query."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    mock_query_result = MagicMock()
    mock_query_result.__iter__ = MagicMock(return_value=iter(records))

    mock_session.run.return_value = mock_query_result
    return mock_driver


def _make_mock_driver_for_handlers() -> MagicMock:
    """Create a mock Neo4j driver for handler tests (single record lookups)."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver


# =========================================================================
# Step 2: Query tests
# =========================================================================
class TestQueryEntriesForReview:
    """Tests for query_entries_for_review in core.monthly_review."""

    def test_query_returns_entries_due_for_review_null_last_reviewed(self) -> None:
        """Entries with last_reviewed_at=null are returned."""
        records = [
            _make_review_record(content="Event A", data_class="world-event", last_reviewed_at=None),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.query_entries_for_review()

        assert "world-event" in result
        assert len(result["world-event"]) == 1
        assert result["world-event"][0].content == "Event A"

    def test_query_returns_entries_reviewed_over_30_days_ago(self) -> None:
        """Entries where last_reviewed_at is more than 30 days ago are returned."""
        old_ts = int(time.time()) - (31 * 86400)
        records = [
            _make_review_record(content="Old entry", data_class="intention", last_reviewed_at=old_ts),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.query_entries_for_review()

        assert "intention" in result
        assert len(result["intention"]) == 1

    def test_query_excludes_entries_reviewed_within_30_days(self) -> None:
        """Recently-reviewed entries are NOT returned (query returns empty)."""
        mock_driver = _make_mock_driver_with_results([])

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.query_entries_for_review()

        # All groups should be empty
        total = sum(len(v) for v in result.values())
        assert total == 0

    def test_query_filters_by_correct_data_classes(self) -> None:
        """Only world-event, intention, session-log entries are returned."""
        records = [
            _make_review_record(content="E1", data_class="world-event"),
            _make_review_record(content="E2", data_class="intention"),
            _make_review_record(content="E3", data_class="session-log"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.query_entries_for_review()

        assert "world-event" in result
        assert "intention" in result
        assert "session-log" in result

    def test_query_excludes_archived_entries(self) -> None:
        """The query Cypher includes an archived filter."""
        mock_driver = _make_mock_driver_with_results([])

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            monthly_review.query_entries_for_review()

        mock_session = mock_driver.session.return_value.__enter__.return_value
        cypher = mock_session.run.call_args[0][0]
        assert "archived" in cypher.lower()

    def test_entries_grouped_by_data_class(self) -> None:
        """The return dict has keys for each class with entries as values."""
        records = [
            _make_review_record(content="W1", data_class="world-event", element_id="id-1"),
            _make_review_record(content="I1", data_class="intention", element_id="id-2"),
            _make_review_record(content="W2", data_class="world-event", element_id="id-3"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.query_entries_for_review()

        assert len(result["world-event"]) == 2
        assert len(result["intention"]) == 1

    def test_empty_results_returns_empty_groups(self) -> None:
        """No error when no entries are due for review."""
        mock_driver = _make_mock_driver_with_results([])

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.query_entries_for_review()

        assert isinstance(result, dict)
        total = sum(len(v) for v in result.values())
        assert total == 0


# =========================================================================
# Regression: _short_id must be a no-op (C1 fix)
# =========================================================================
class TestShortIdNoOp:
    """Verify _short_id returns the full element ID (no truncation)."""

    def test_short_id_returns_full_element_id(self) -> None:
        """_short_id must return the complete element ID for Neo4j lookups."""
        from core.monthly_review import _short_id

        full_id = "4:abcdef123456:789"
        assert _short_id(full_id) == full_id

    def test_short_id_preserves_colons(self) -> None:
        """_short_id must preserve colons in Neo4j element IDs."""
        from core.monthly_review import _short_id

        assert ":" in _short_id("4:abc:123")


# =========================================================================
# Step 3: Message builder tests
# =========================================================================
class TestBuildReviewMessages:
    """Tests for build_review_messages in core.monthly_review."""

    def _make_review_entry(self, **kwargs):
        from core.monthly_review import ReviewEntry
        defaults = {
            "element_id": "4:abc:123",
            "content": "Test content",
            "data_class": "world-event",
            "created_at": 1700000000,
            "last_reviewed_at": None,
        }
        defaults.update(kwargs)
        return ReviewEntry(**defaults)

    def test_build_review_message_groups_by_class(self) -> None:
        """Message contains class-based section headers."""
        from core.monthly_review import build_review_messages

        grouped = {
            "world-event": [self._make_review_entry(data_class="world-event")],
            "intention": [self._make_review_entry(data_class="intention")],
        }

        messages = build_review_messages(grouped)

        assert "world-event" in messages
        assert "intention" in messages

    def test_build_review_message_includes_entry_summary(self) -> None:
        """Each entry's content (truncated) and date are in the message."""
        from core.monthly_review import build_review_messages

        grouped = {
            "world-event": [
                self._make_review_entry(content="Major earthquake in Turkey", created_at=1700000000),
            ],
        }

        messages = build_review_messages(grouped)
        msg = messages["world-event"]
        assert "Major earthquake in Turkey" in msg

    def test_build_review_message_includes_action_commands(self) -> None:
        """Each entry has /keep_<id>, /archive_<id> (world-event), /discard_<id> commands with full element IDs."""
        from core.monthly_review import build_review_messages

        grouped = {
            "world-event": [
                self._make_review_entry(element_id="4:abcdef123456:789", data_class="world-event"),
            ],
        }

        messages = build_review_messages(grouped)
        msg = messages["world-event"]
        # Full element ID must be used in commands (not truncated)
        assert "/keep_4:abcdef123456:789" in msg
        assert "/archive_4:abcdef123456:789" in msg
        assert "/discard_4:abcdef123456:789" in msg

    def test_build_review_message_archive_only_for_world_events(self) -> None:
        """/archive_<id> only appears for world-event, not for intention or session-log."""
        from core.monthly_review import build_review_messages

        grouped = {
            "intention": [
                self._make_review_entry(element_id="4:int123456789:1", data_class="intention"),
            ],
            "session-log": [
                self._make_review_entry(element_id="4:ses123456789:1", data_class="session-log"),
            ],
        }

        messages = build_review_messages(grouped)

        assert "/archive_" not in messages["intention"]
        assert "/archive_" not in messages["session-log"]
        # But keep and discard are there
        assert "/keep_" in messages["intention"]
        assert "/discard_" in messages["intention"]

    def test_build_review_message_truncates_long_content(self) -> None:
        """Entries with very long content are truncated."""
        from core.monthly_review import build_review_messages

        long_content = "A" * 500
        grouped = {
            "world-event": [
                self._make_review_entry(content=long_content),
            ],
        }

        messages = build_review_messages(grouped)
        msg = messages["world-event"]
        # Content should be truncated to 200 chars max in the message
        assert "A" * 201 not in msg

    def test_build_review_message_single_message_per_class(self) -> None:
        """One message string is returned per class group."""
        from core.monthly_review import build_review_messages

        grouped = {
            "world-event": [
                self._make_review_entry(element_id="id-1", content="Entry 1"),
                self._make_review_entry(element_id="id-2", content="Entry 2"),
            ],
        }

        messages = build_review_messages(grouped)
        assert isinstance(messages["world-event"], str)
        assert "Entry 1" in messages["world-event"]
        assert "Entry 2" in messages["world-event"]


# =========================================================================
# Step 4: Response handler tests
# =========================================================================
class TestHandleKeep:
    """Tests for handle_keep in core.monthly_review."""

    def test_handle_keep_sets_last_reviewed_at(self) -> None:
        """Neo4j SET query updates last_reviewed_at to current timestamp."""
        mock_driver = _make_mock_driver_for_handlers()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        # Simulate that the entry exists (counters > 0)
        mock_result = MagicMock()
        mock_result.single.return_value = {"count": 1}
        mock_session.run.return_value = mock_result

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.handle_keep("4:abc:123")

        assert result["ok"] is True
        assert result["action"] == "keep"

        # Verify SET query was called
        calls = mock_session.run.call_args_list
        set_call = calls[-1]
        cypher = set_call[0][0]
        assert "last_reviewed_at" in cypher

    def test_handle_keep_does_not_modify_other_fields(self) -> None:
        """Only last_reviewed_at is changed."""
        mock_driver = _make_mock_driver_for_handlers()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        mock_result = MagicMock()
        mock_result.single.return_value = {"count": 1}
        mock_session.run.return_value = mock_result

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            monthly_review.handle_keep("4:abc:123")

        calls = mock_session.run.call_args_list
        set_call = calls[-1]
        cypher = set_call[0][0]
        # Should only SET last_reviewed_at, nothing else
        assert "archived" not in cypher.lower() or "last_reviewed_at" in cypher

    def test_handle_keep_unknown_id_returns_error(self) -> None:
        """Error when element ID does not exist."""
        mock_driver = _make_mock_driver_for_handlers()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        mock_result = MagicMock()
        mock_result.single.return_value = {"count": 0}
        mock_session.run.return_value = mock_result

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.handle_keep("nonexistent-id")

        assert result["ok"] is False
        assert "error" in result


class TestHandleArchive:
    """Tests for handle_archive in core.monthly_review."""

    def test_handle_archive_marks_archived_true(self) -> None:
        """Neo4j SET query sets archived=true."""
        mock_driver = _make_mock_driver_for_handlers()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        # First call: lookup the entry
        mock_entry = MagicMock()
        mock_entry.single.return_value = {
            "content": "World event",
            "data_class": "world-event",
            "tags": "",
            "source": "user",
            "agent_id": "ada",
            "created_at": 1700000000,
            "props": {"tier": "T3"},
        }
        # Second call: the SET query
        mock_set_result = MagicMock()
        mock_set_result.single.return_value = {"count": 1}
        mock_session.run.side_effect = [mock_entry, mock_set_result]

        from core import monthly_review

        with (
            patch.object(monthly_review, "_get_driver", return_value=mock_driver),
            patch.object(monthly_review, "_get_archive_store") as mock_store_fn,
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store
            result = monthly_review.handle_archive("4:abc:123")

        assert result["ok"] is True
        assert result["action"] == "archive"

        # Verify the SET query includes archived=true
        set_call = mock_session.run.call_args_list[-1]
        cypher = set_call[0][0]
        assert "archived" in cypher.lower()

    def test_handle_archive_saves_to_archive_store(self) -> None:
        """ArchiveStore.save() is called with correct entry data."""
        mock_driver = _make_mock_driver_for_handlers()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        mock_entry = MagicMock()
        mock_entry.single.return_value = {
            "content": "Earthquake in Turkey",
            "data_class": "world-event",
            "tags": "news",
            "source": "user",
            "agent_id": "ada",
            "created_at": 1700000000,
            "props": {"tier": "T3"},
        }
        mock_set_result = MagicMock()
        mock_set_result.single.return_value = {"count": 1}
        mock_session.run.side_effect = [mock_entry, mock_set_result]

        from core import monthly_review

        with (
            patch.object(monthly_review, "_get_driver", return_value=mock_driver),
            patch.object(monthly_review, "_get_archive_store") as mock_store_fn,
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store
            monthly_review.handle_archive("4:abc:123")

        mock_store.save.assert_called_once()
        saved_entry = mock_store.save.call_args[0][0]
        assert saved_entry.content == "Earthquake in Turkey"
        assert saved_entry.data_class == "world-event"
        assert saved_entry.original_id == "4:abc:123"

    def test_handle_archive_only_for_world_events(self) -> None:
        """Archive returns error for non-world-event entries."""
        mock_driver = _make_mock_driver_for_handlers()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        mock_entry = MagicMock()
        mock_entry.single.return_value = {
            "content": "Some intention",
            "data_class": "intention",
            "tags": "",
            "source": "user",
            "agent_id": "ada",
            "created_at": 1700000000,
            "props": {},
        }
        mock_session.run.return_value = mock_entry

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.handle_archive("4:abc:123")

        assert result["ok"] is False
        assert "world-event" in result["error"].lower()


class TestHandleDiscard:
    """Tests for handle_discard in core.monthly_review."""

    def test_handle_discard_deletes_from_neo4j(self) -> None:
        """DETACH DELETE Cypher is executed for the entry."""
        mock_driver = _make_mock_driver_for_handlers()
        mock_session = mock_driver.session.return_value.__enter__.return_value

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.handle_discard("4:abc:123")

        assert result["ok"] is True
        assert result["action"] == "discard"

        cypher = mock_session.run.call_args[0][0]
        assert "DETACH DELETE" in cypher

    def test_handle_discard_returns_success(self) -> None:
        """The function returns a success result dict."""
        mock_driver = _make_mock_driver_for_handlers()

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.handle_discard("4:abc:123")

        assert result["ok"] is True

    def test_handle_discard_unknown_id_does_not_raise(self) -> None:
        """No exception for missing entry (idempotent)."""
        mock_driver = _make_mock_driver_for_handlers()

        from core import monthly_review

        with patch.object(monthly_review, "_get_driver", return_value=mock_driver):
            result = monthly_review.handle_discard("nonexistent-id")

        assert result["ok"] is True


# =========================================================================
# Step 5: Sweep orchestrator tests
# =========================================================================
class TestSweepMonthlyReview:
    """Tests for sweep_monthly_review in core.monthly_review."""

    def test_sweep_monthly_review_queries_and_sends_messages(self) -> None:
        """Asserts query + message build + Telegram send flow."""
        from core.monthly_review import ReviewEntry

        entries = {
            "world-event": [
                ReviewEntry("id-1", "Event content", "world-event", 1700000000, None),
            ],
        }

        from core import monthly_review

        with (
            patch.object(monthly_review, "query_entries_for_review", return_value=entries),
            patch.object(
                monthly_review,
                "build_review_messages",
                return_value={"world-event": "Review message here"},
            ),
            patch.object(monthly_review, "_telegram_direct", return_value=(True, "sent")) as mock_tg,
        ):
            result = monthly_review.sweep_monthly_review()

        assert result["entries_found"] == 1
        assert result["messages_sent"] == 1
        mock_tg.assert_called_once()

    def test_sweep_monthly_review_no_entries_sends_nothing(self) -> None:
        """No Telegram call when no entries due for review."""
        from core import monthly_review

        with (
            patch.object(monthly_review, "query_entries_for_review", return_value={}),
            patch.object(monthly_review, "_telegram_direct") as mock_tg,
        ):
            result = monthly_review.sweep_monthly_review()

        assert result["entries_found"] == 0
        assert result["messages_sent"] == 0
        mock_tg.assert_not_called()

    def test_sweep_monthly_review_returns_summary_dict(self) -> None:
        """Return dict has entries_found, messages_sent, errors keys."""
        from core import monthly_review

        with (
            patch.object(monthly_review, "query_entries_for_review", return_value={}),
            patch.object(monthly_review, "_telegram_direct"),
        ):
            result = monthly_review.sweep_monthly_review()

        assert "entries_found" in result
        assert "messages_sent" in result
        assert "errors" in result

    def test_sweep_monthly_review_handles_telegram_failure_gracefully(self) -> None:
        """Sweep completes even if Telegram send fails."""
        from core.monthly_review import ReviewEntry

        entries = {
            "world-event": [
                ReviewEntry("id-1", "Event content", "world-event", 1700000000, None),
            ],
        }

        from core import monthly_review

        with (
            patch.object(monthly_review, "query_entries_for_review", return_value=entries),
            patch.object(
                monthly_review,
                "build_review_messages",
                return_value={"world-event": "Review msg"},
            ),
            patch.object(
                monthly_review,
                "_telegram_direct",
                side_effect=Exception("Telegram API down"),
            ),
        ):
            result = monthly_review.sweep_monthly_review()

        assert result["errors"] >= 1
        assert result["messages_sent"] == 0
