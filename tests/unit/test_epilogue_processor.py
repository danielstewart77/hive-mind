"""Tests for the main epilogue processor orchestrator."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.epilogue import process_session_epilogue, archive_transcript


class TestProcessSkipsAlreadyProcessed:
    """Verify sessions with epilogue_status set are handled correctly."""

    @pytest.mark.asyncio
    async def test_process_skips_already_processed_session(self):
        mock_db = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(
            return_value={"epilogue_status": "completed", "claude_sid": "abc", "summary": "", "last_active": 0.0}
        )
        mock_db.execute = AsyncMock(return_value=mock_cursor)

        result = await process_session_epilogue(
            "session-1", mock_db, "http://localhost:8420", AsyncMock(), 123
        )
        assert result == "completed"


class TestProcessMarksLowSignalAsSkipped:
    """Verify low-signal sessions are marked as skipped."""

    @pytest.mark.asyncio
    async def test_process_marks_low_signal_as_skipped(self, tmp_path):
        # Create a transcript with only 2 user turns (below threshold)
        transcript = tmp_path / "claude-sid-123.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "Hi"}, "timestamp": "t1"}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}, "timestamp": "t2"}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "Bye"}, "timestamp": "t3"}),
        ]
        transcript.write_text("\n".join(lines) + "\n")

        mock_db = AsyncMock()
        call_count = 0

        async def mock_execute(query, params=None):
            nonlocal call_count
            call_count += 1
            cursor = AsyncMock()
            if "SELECT" in query:
                cursor.fetchone = AsyncMock(
                    return_value={"epilogue_status": None, "claude_sid": "claude-sid-123", "summary": "", "last_active": 0.0}
                )
            return cursor

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        with patch("core.epilogue.TRANSCRIPT_DIR", tmp_path):
            result = await process_session_epilogue(
                "session-1", mock_db, "http://localhost:8420", AsyncMock(), 123
            )
        assert result == "skipped"


class TestProcessStatusTransitions:
    """Verify status transitions through the processing pipeline."""

    @pytest.mark.asyncio
    async def test_process_marks_pending_before_digest(self, tmp_path):
        # Create a substantive transcript
        transcript = tmp_path / "claude-sid-456.jsonl"
        lines = []
        for i in range(5):
            lines.append(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"Discussing architecture topic {i}"},
                "timestamp": f"t{i*2}",
            }))
            lines.append(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": f"Response about topic {i}"}]},
                "timestamp": f"t{i*2+1}",
            }))
        transcript.write_text("\n".join(lines) + "\n")

        status_updates = []
        mock_db = AsyncMock()

        async def mock_execute(query, params=None):
            cursor = AsyncMock()
            if "SELECT" in query:
                cursor.fetchone = AsyncMock(
                    return_value={"epilogue_status": None, "claude_sid": "claude-sid-456", "summary": "", "last_active": 0.0}
                )
            elif "UPDATE" in query and "epilogue_status" in query and params:
                # Track status updates
                if isinstance(params, tuple) and len(params) >= 1:
                    status_updates.append(query)
            return cursor

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        mock_gateway = AsyncMock()
        mock_gateway.query = AsyncMock(return_value=json.dumps({
            "digest": "Test digest",
            "topics": ["topic1"],
            "entities": [],
        }))

        with (
            patch("core.epilogue.TRANSCRIPT_DIR", tmp_path),
            patch("core.epilogue.request_digest_approval", new_callable=AsyncMock, return_value=True),
            patch("core.epilogue.archive_transcript"),
        ):
            result = await process_session_epilogue(
                "session-1", mock_db, "http://localhost:8420", mock_gateway, 123
            )

        # Verify "pending" was set (first UPDATE with epilogue_status)
        assert any("pending" in q for q in status_updates)

    @pytest.mark.asyncio
    async def test_process_marks_completed_on_approval(self, tmp_path):
        transcript = tmp_path / "claude-sid-789.jsonl"
        lines = []
        for i in range(5):
            lines.append(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"Architecture discussion point {i}"},
                "timestamp": f"t{i*2}",
            }))
            lines.append(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": f"Response {i}"}]},
                "timestamp": f"t{i*2+1}",
            }))
        transcript.write_text("\n".join(lines) + "\n")

        mock_db = AsyncMock()

        async def mock_execute(query, params=None):
            cursor = AsyncMock()
            if "SELECT" in query:
                cursor.fetchone = AsyncMock(
                    return_value={"epilogue_status": None, "claude_sid": "claude-sid-789", "summary": "", "last_active": 0.0}
                )
            return cursor

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        mock_gateway = AsyncMock()
        mock_gateway.query = AsyncMock(return_value=json.dumps({
            "digest": "Test digest",
            "topics": ["topic1"],
            "entities": [],
        }))

        with (
            patch("core.epilogue.TRANSCRIPT_DIR", tmp_path),
            patch("core.epilogue.request_digest_approval", new_callable=AsyncMock, return_value=True),
            patch("core.epilogue.archive_transcript"),
        ):
            result = await process_session_epilogue(
                "session-1", mock_db, "http://localhost:8420", mock_gateway, 123
            )
        assert result == "completed"

    @pytest.mark.asyncio
    async def test_process_marks_skipped_on_denial(self, tmp_path):
        transcript = tmp_path / "claude-sid-101.jsonl"
        lines = []
        for i in range(5):
            lines.append(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"Architecture discussion point {i}"},
                "timestamp": f"t{i*2}",
            }))
            lines.append(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": f"Response {i}"}]},
                "timestamp": f"t{i*2+1}",
            }))
        transcript.write_text("\n".join(lines) + "\n")

        mock_db = AsyncMock()

        async def mock_execute(query, params=None):
            cursor = AsyncMock()
            if "SELECT" in query:
                cursor.fetchone = AsyncMock(
                    return_value={"epilogue_status": None, "claude_sid": "claude-sid-101", "summary": "", "last_active": 0.0}
                )
            return cursor

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        mock_gateway = AsyncMock()
        mock_gateway.query = AsyncMock(return_value=json.dumps({
            "digest": "Test digest",
            "topics": ["topic1"],
            "entities": [],
        }))

        with (
            patch("core.epilogue.TRANSCRIPT_DIR", tmp_path),
            patch("core.epilogue.request_digest_approval", new_callable=AsyncMock, return_value=False),
            patch("core.epilogue.archive_transcript"),
        ):
            result = await process_session_epilogue(
                "session-1", mock_db, "http://localhost:8420", mock_gateway, 123
            )
        assert result == "skipped"


class TestArchiveTranscript:
    """Verify transcript archival behavior."""

    def test_archive_transcript_moves_file(self, tmp_path):
        source = tmp_path / "test.jsonl"
        source.write_text('{"type": "user"}')

        archive_dir = tmp_path / "archive"
        with patch("core.epilogue.ARCHIVE_DIR", archive_dir):
            archive_transcript(source, "session-123")

        assert not source.exists()
        assert (archive_dir / "session-123.jsonl").exists()

    def test_archive_call_site_has_temporary_comment(self):
        """Verify the archive call site has the required TEMPORARY comment."""
        import inspect
        from core import epilogue

        source = inspect.getsource(epilogue)
        assert "# TEMPORARY: Phase 1 only" in source
