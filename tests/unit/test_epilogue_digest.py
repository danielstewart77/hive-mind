"""Tests for digest prompt building in core/epilogue.py."""

import pytest

from core.epilogue import (
    TranscriptTurn,
    build_digest_prompt,
    MAX_TRANSCRIPT_CHARS,
)


class TestBuildDigestPrompt:
    """Tests for build_digest_prompt function."""

    def test_build_digest_prompt_includes_transcript_content(self):
        turns = [
            TranscriptTurn(role="user", content="Hello there", timestamp="t1"),
            TranscriptTurn(role="assistant", content="Hi!", timestamp="t2"),
        ]
        prompt = build_digest_prompt(turns)
        assert "Hello there" in prompt
        assert "Hi!" in prompt
        assert "---BEGIN TRANSCRIPT---" in prompt
        assert "---END TRANSCRIPT---" in prompt
        assert "[User]:" in prompt
        assert "[Assistant]:" in prompt

    def test_build_digest_prompt_chunks_large_transcripts(self):
        # Create a transcript that exceeds MAX_TRANSCRIPT_CHARS
        long_content = "A" * (MAX_TRANSCRIPT_CHARS + 1000)
        turns = [
            TranscriptTurn(role="user", content=long_content, timestamp="t1"),
        ]
        prompt = build_digest_prompt(turns)
        assert "TRANSCRIPT TRUNCATED" in prompt
        # The prompt should still be well-formed
        assert "---BEGIN TRANSCRIPT---" in prompt
        assert "---END TRANSCRIPT---" in prompt

    def test_build_digest_prompt_requests_structured_output(self):
        turns = [
            TranscriptTurn(role="user", content="Test content", timestamp="t1"),
        ]
        prompt = build_digest_prompt(turns)
        assert "JSON digest" in prompt or "Produce the JSON" in prompt
