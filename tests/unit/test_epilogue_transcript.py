"""Tests for transcript parsing and triage logic in core/epilogue.py."""

import json
import os
from pathlib import Path

import pytest


class TestParseTranscript:
    """Tests for parse_transcript function."""

    def test_parse_transcript_extracts_user_and_assistant_turns(self):
        from core.epilogue import parse_transcript

        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Hello, how are you?"},
                "timestamp": "2026-03-02T10:00:00Z",
            }),
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "I am doing well, thanks!"}
                ]},
                "timestamp": "2026-03-02T10:00:01Z",
            }),
        ]
        turns = parse_transcript(lines)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[0].content == "Hello, how are you?"
        assert turns[1].role == "assistant"
        assert turns[1].content == "I am doing well, thanks!"

    def test_parse_transcript_handles_multimodal_content(self):
        from core.epilogue import parse_transcript

        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "text", "text": "Look at this image"},
                    {"type": "image", "source": {"type": "base64", "data": "abc"}},
                    {"type": "text", "text": " and tell me what you see"},
                ]},
                "timestamp": "2026-03-02T10:00:00Z",
            }),
        ]
        turns = parse_transcript(lines)
        assert len(turns) == 1
        assert turns[0].content == "Look at this image and tell me what you see"

    def test_parse_transcript_skips_non_message_events(self):
        from core.epilogue import parse_transcript

        lines = [
            json.dumps({"type": "queue-operation", "data": "something"}),
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Hello"},
                "timestamp": "2026-03-02T10:00:00Z",
            }),
            json.dumps({"type": "result", "result": "done"}),
            json.dumps({"type": "system", "system": "init"}),
        ]
        turns = parse_transcript(lines)
        assert len(turns) == 1
        assert turns[0].role == "user"

    def test_parse_transcript_handles_empty_lines(self):
        from core.epilogue import parse_transcript

        lines = [
            "",
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Hello"},
                "timestamp": "2026-03-02T10:00:00Z",
            }),
            "",
        ]
        turns = parse_transcript(lines)
        assert len(turns) == 1


class TestCountUserTurns:
    """Tests for count_user_turns function."""

    def test_count_user_turns(self):
        from core.epilogue import count_user_turns, TranscriptTurn

        turns = [
            TranscriptTurn(role="user", content="Hello", timestamp="t1"),
            TranscriptTurn(role="assistant", content="Hi", timestamp="t2"),
            TranscriptTurn(role="user", content="Bye", timestamp="t3"),
            TranscriptTurn(role="assistant", content="Bye!", timestamp="t4"),
            TranscriptTurn(role="user", content="Wait", timestamp="t5"),
        ]
        assert count_user_turns(turns) == 3

    def test_count_user_turns_empty(self):
        from core.epilogue import count_user_turns

        assert count_user_turns([]) == 0


class TestTriageSession:
    """Tests for triage_session function."""

    def test_triage_skips_session_under_3_turns(self):
        from core.epilogue import triage_session, TranscriptTurn

        turns = [
            TranscriptTurn(role="user", content="Hello", timestamp="t1"),
            TranscriptTurn(role="assistant", content="Hi", timestamp="t2"),
            TranscriptTurn(role="user", content="Bye", timestamp="t3"),
            TranscriptTurn(role="assistant", content="Bye!", timestamp="t4"),
        ]
        should_skip, reason = triage_session(turns)
        assert should_skip is True
        assert "fewer than 3 user turns" in reason

    def test_triage_skips_pure_utility_session(self):
        from core.epilogue import triage_session, TranscriptTurn

        turns = [
            TranscriptTurn(role="user", content="What's the weather today?", timestamp="t1"),
            TranscriptTurn(role="assistant", content="75 degrees", timestamp="t2"),
            TranscriptTurn(role="user", content="What time is it?", timestamp="t3"),
            TranscriptTurn(role="assistant", content="3pm", timestamp="t4"),
            TranscriptTurn(role="user", content="Current time please", timestamp="t5"),
            TranscriptTurn(role="assistant", content="3:01pm", timestamp="t6"),
        ]
        should_skip, reason = triage_session(turns)
        assert should_skip is True
        assert "utility" in reason.lower()

    def test_triage_passes_substantive_session(self):
        from core.epilogue import triage_session, TranscriptTurn

        turns = [
            TranscriptTurn(role="user", content="Let's discuss the new architecture for the payment service", timestamp="t1"),
            TranscriptTurn(role="assistant", content="Great, I'd suggest a microservices approach", timestamp="t2"),
            TranscriptTurn(role="user", content="What about using event sourcing?", timestamp="t3"),
            TranscriptTurn(role="assistant", content="Event sourcing is great for audit trails", timestamp="t4"),
            TranscriptTurn(role="user", content="And we should consider CQRS too", timestamp="t5"),
            TranscriptTurn(role="assistant", content="Yes, CQRS pairs well with event sourcing", timestamp="t6"),
            TranscriptTurn(role="user", content="Let's go with that. Can you write the design doc?", timestamp="t7"),
            TranscriptTurn(role="assistant", content="Sure, here's the design doc...", timestamp="t8"),
        ]
        should_skip, reason = triage_session(turns)
        assert should_skip is False
        assert reason == ""


class TestReadTranscriptFile:
    """Tests for read_transcript_file function."""

    def test_read_transcript_file_parses_jsonl(self, tmp_path):
        from core.epilogue import read_transcript_file

        jsonl_file = tmp_path / "test.jsonl"
        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "Hello"},
                "timestamp": "2026-03-02T10:00:00Z",
            }),
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Hi there!"}
                ]},
                "timestamp": "2026-03-02T10:00:01Z",
            }),
        ]
        jsonl_file.write_text("\n".join(lines) + "\n")

        turns = read_transcript_file(jsonl_file)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[1].content == "Hi there!"

    def test_read_transcript_file_returns_empty_on_missing(self, tmp_path):
        from core.epilogue import read_transcript_file

        missing = tmp_path / "nonexistent.jsonl"
        turns = read_transcript_file(missing)
        assert turns == []
