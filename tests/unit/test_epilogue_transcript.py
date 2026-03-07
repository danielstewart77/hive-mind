"""Tests for transcript reading and formatting in core/epilogue.py."""

import json
from pathlib import Path


class TestReadTranscript:
    """Tests for read_transcript(path)."""

    def test_reads_user_and_assistant_turns(self, tmp_path):
        from core.epilogue import read_transcript

        f = tmp_path / "test.jsonl"
        f.write_text("\n".join([
            json.dumps({"type": "user", "message": {"role": "user", "content": "Hello"}, "timestamp": "t1"}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}]}, "timestamp": "t2"}),
        ]))
        turns = read_transcript(f)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[0].content == "Hello"
        assert turns[1].role == "assistant"
        assert turns[1].content == "Hi!"

    def test_skips_non_message_events(self, tmp_path):
        from core.epilogue import read_transcript

        f = tmp_path / "test.jsonl"
        f.write_text("\n".join([
            json.dumps({"type": "queue-operation", "data": "x"}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "Hello"}, "timestamp": "t1"}),
            json.dumps({"type": "result", "result": "done"}),
        ]))
        turns = read_transcript(f)
        assert len(turns) == 1
        assert turns[0].content == "Hello"

    def test_handles_multimodal_content(self, tmp_path):
        from core.epilogue import read_transcript

        f = tmp_path / "test.jsonl"
        f.write_text(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "Look at this "},
                {"type": "image", "source": {}},
                {"type": "text", "text": "image"},
            ]},
            "timestamp": "t1",
        }))
        turns = read_transcript(f)
        assert len(turns) == 1
        assert turns[0].content == "Look at this image"

    def test_returns_empty_list_when_file_missing(self, tmp_path):
        from core.epilogue import read_transcript

        turns = read_transcript(tmp_path / "nonexistent.jsonl")
        assert turns == []

    def test_skips_empty_lines(self, tmp_path):
        from core.epilogue import read_transcript

        f = tmp_path / "test.jsonl"
        f.write_text(
            "\n"
            + json.dumps({"type": "user", "message": {"role": "user", "content": "Hello"}, "timestamp": "t1"})
            + "\n\n"
        )
        turns = read_transcript(f)
        assert len(turns) == 1


class TestFormatTranscript:
    """Tests for format_transcript(turns)."""

    def test_formats_turns_with_labels(self):
        from core.epilogue import TranscriptTurn, format_transcript

        turns = [
            TranscriptTurn(role="user", content="What is 2+2?", timestamp="t1"),
            TranscriptTurn(role="assistant", content="4.", timestamp="t2"),
        ]
        text = format_transcript(turns)
        assert "[User]: What is 2+2?" in text
        assert "[Assistant]: 4." in text

    def test_truncates_long_transcripts(self):
        from core.epilogue import TranscriptTurn, format_transcript, MAX_TRANSCRIPT_CHARS

        long_content = "x" * (MAX_TRANSCRIPT_CHARS + 1000)
        turns = [TranscriptTurn(role="user", content=long_content, timestamp="t1")]
        text = format_transcript(turns)
        assert "TRUNCATED" in text
        assert len(text) <= MAX_TRANSCRIPT_CHARS + 100  # a little slack for the truncation suffix

    def test_empty_turns_returns_empty_string(self):
        from core.epilogue import format_transcript

        assert format_transcript([]) == ""
