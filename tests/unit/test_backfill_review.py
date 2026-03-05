"""Unit tests for backfill review formatting and parsing — pure logic, no I/O."""

import pytest

from core.backfill_review import format_review_batch, parse_classify_command


class TestFormatReviewBatch:
    """Tests for format_review_batch message formatting."""

    def test_format_review_batch_groups_entries(self) -> None:
        from agents.memory_backfill import BackfillEntry
        from core.backfill_classifier import ClassificationResult

        entries = [
            (
                BackfillEntry("4:a:0", "Content one", "", None, "user", "memory", None),
                ClassificationResult("person", 0.5, "low confidence", ["person", "preference"]),
            ),
            (
                BackfillEntry("4:a:1", "Content two", "", None, "user", "memory", None),
                ClassificationResult("preference", 0.4, "ambiguous", ["preference", "intention"]),
            ),
        ]
        messages = format_review_batch(entries)
        assert len(messages) >= 1
        # Both entries should appear in the messages
        combined = "\n".join(messages)
        assert "Content one" in combined
        assert "Content two" in combined

    def test_format_review_batch_truncates_content(self) -> None:
        from agents.memory_backfill import BackfillEntry
        from core.backfill_classifier import ClassificationResult

        long_content = "A" * 500
        entries = [
            (
                BackfillEntry("4:a:0", long_content, "", None, "user", "memory", None),
                ClassificationResult("person", 0.5, "low", ["person"]),
            ),
        ]
        messages = format_review_batch(entries)
        combined = "\n".join(messages)
        # Content should be truncated to 200 chars
        assert "A" * 201 not in combined
        assert "A" * 50 in combined  # But some of it should be there

    def test_format_review_batch_shows_candidates(self) -> None:
        from agents.memory_backfill import BackfillEntry
        from core.backfill_classifier import ClassificationResult

        entries = [
            (
                BackfillEntry("4:a:0", "Test", "", None, "user", "memory", None),
                ClassificationResult("person", 0.5, "ambiguous", ["person", "preference", "intention"]),
            ),
        ]
        messages = format_review_batch(entries)
        combined = "\n".join(messages)
        assert "person" in combined
        assert "preference" in combined

    def test_format_review_batch_includes_entry_id(self) -> None:
        from agents.memory_backfill import BackfillEntry
        from core.backfill_classifier import ClassificationResult

        entries = [
            (
                BackfillEntry("4:a:0", "Test content", "", None, "user", "memory", None),
                ClassificationResult("person", 0.5, "test", ["person"]),
            ),
        ]
        messages = format_review_batch(entries)
        combined = "\n".join(messages)
        # The entry should have some form of ID for reference
        assert "4:a:0" in combined or "/classify_" in combined

    def test_format_review_message_header(self) -> None:
        from agents.memory_backfill import BackfillEntry
        from core.backfill_classifier import ClassificationResult

        entries = [
            (
                BackfillEntry("4:a:0", "Test", "", None, "user", "memory", None),
                ClassificationResult("person", 0.5, "test", ["person"]),
            ),
            (
                BackfillEntry("4:a:1", "Test 2", "", None, "user", "memory", None),
                ClassificationResult("preference", 0.4, "test", ["preference"]),
            ),
        ]
        messages = format_review_batch(entries)
        # First message should contain count info
        assert "2" in messages[0]

    def test_format_empty_review_batch(self) -> None:
        messages = format_review_batch([])
        assert len(messages) == 1
        assert "no entries" in messages[0].lower() or "all" in messages[0].lower()


class TestParseClassifyCommand:
    """Tests for parse_classify_command text parsing."""

    def test_parse_classify_command_valid(self) -> None:
        result = parse_classify_command("/classify_4:a:0 person")
        assert result is not None
        entry_id, data_class = result
        assert entry_id == "4:a:0"
        assert data_class == "person"

    def test_parse_classify_command_invalid_class(self) -> None:
        # The parser accepts any class string -- downstream validation rejects invalid classes
        result = parse_classify_command("/classify_4:a:0 invalid-unknown-class")
        assert result == ("4:a:0", "invalid-unknown-class")

    def test_parse_classify_command_new_class(self) -> None:
        result = parse_classify_command("/classify_4:a:0 new:shopping-list")
        assert result is not None
        entry_id, data_class = result
        assert entry_id == "4:a:0"
        assert data_class == "new:shopping-list"

    def test_parse_classify_command_malformed(self) -> None:
        result = parse_classify_command("/classify_")
        assert result is None

    def test_parse_classify_command_no_class(self) -> None:
        result = parse_classify_command("/classify_4:a:0")
        assert result is None
