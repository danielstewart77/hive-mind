"""Unit tests for transcript parsing and metrics extraction."""

import json
from pathlib import Path

import pytest

from core.epilogue import parse_transcript


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    """Write a list of dicts as JSONL to a file."""
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


class TestParseTranscript:
    """Tests for parse_transcript() function."""

    def test_counts_user_turns(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": "Hello"}, "timestamp": "2026-01-01T10:00:00Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]}, "timestamp": "2026-01-01T10:01:00Z"},
            {"type": "user", "message": {"role": "user", "content": "How are you?"}, "timestamp": "2026-01-01T10:02:00Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Good"}]}, "timestamp": "2026-01-01T10:03:00Z"},
        ])
        turn_count, _, _ = parse_transcript(path)
        assert turn_count == 2

    def test_calculates_duration(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": "Start"}, "timestamp": "2026-01-01T10:00:00Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "End"}]}, "timestamp": "2026-01-01T10:30:00Z"},
        ])
        _, duration_minutes, _ = parse_transcript(path)
        assert duration_minutes == pytest.approx(30.0, abs=0.1)

    def test_extracts_conversation_text(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": "Tell me about trees"}, "timestamp": "2026-01-01T10:00:00Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Trees are tall plants"}]}, "timestamp": "2026-01-01T10:01:00Z"},
        ])
        _, _, text = parse_transcript(path)
        assert "Tell me about trees" in text
        assert "Trees are tall plants" in text

    def test_empty_file_returns_zero_metrics(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.jsonl"
        path.write_text("")
        turn_count, duration_minutes, text = parse_transcript(path)
        assert turn_count == 0
        assert duration_minutes == 0.0
        assert text == ""

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            parse_transcript(Path("/nonexistent/transcript.jsonl"))

    def test_single_message(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": "Just one"}, "timestamp": "2026-01-01T10:00:00Z"},
        ])
        turn_count, duration_minutes, text = parse_transcript(path)
        assert turn_count == 1
        assert duration_minutes == 0.0
        assert "Just one" in text

    def test_skips_non_message_lines(self, tmp_path: Path) -> None:
        """Non-user/assistant lines (progress, queue-operation, etc.) are ignored."""
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-01-01T09:59:00Z"},
            {"type": "progress", "data": {"type": "hook_progress"}, "timestamp": "2026-01-01T09:59:30Z"},
            {"type": "user", "message": {"role": "user", "content": "Hello"}, "timestamp": "2026-01-01T10:00:00Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]}, "timestamp": "2026-01-01T10:05:00Z"},
        ])
        turn_count, duration_minutes, text = parse_transcript(path)
        assert turn_count == 1
        assert duration_minutes == pytest.approx(5.0, abs=0.1)
