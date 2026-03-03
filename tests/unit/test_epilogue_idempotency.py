"""Tests for epilogue idempotency: completed/skipped not re-processed, pending/digest_sent allow retry."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.epilogue import process_session_epilogue


class TestIdempotency:
    """Verify idempotency guarantees for epilogue processing."""

    @pytest.mark.asyncio
    async def test_completed_session_not_reprocessed(self):
        """Sessions with epilogue_status='completed' should return immediately."""
        mock_db = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(
            return_value={"epilogue_status": "completed", "claude_sid": "abc"}
        )
        mock_db.execute = AsyncMock(return_value=mock_cursor)

        result = await process_session_epilogue(
            "session-1", mock_db, "http://localhost:8420", AsyncMock(), 123
        )
        assert result == "completed"
        # Should NOT have called commit (no status changes)
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_session_not_reprocessed(self):
        """Sessions with epilogue_status='skipped' should return immediately."""
        mock_db = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(
            return_value={"epilogue_status": "skipped", "claude_sid": "abc"}
        )
        mock_db.execute = AsyncMock(return_value=mock_cursor)

        result = await process_session_epilogue(
            "session-2", mock_db, "http://localhost:8420", AsyncMock(), 123
        )
        assert result == "skipped"
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_session_can_be_retried(self, tmp_path):
        """Sessions with epilogue_status='pending' should be retried (crashed mid-process)."""
        # Create a transcript
        transcript = tmp_path / "claude-pending.jsonl"
        lines = []
        for i in range(5):
            lines.append(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"Architecture point {i}"},
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
                    return_value={"epilogue_status": "pending", "claude_sid": "claude-pending"}
                )
            return cursor

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        mock_gateway = AsyncMock()
        mock_gateway.query = AsyncMock(return_value=json.dumps({
            "digest": "Retry digest",
            "topics": ["topic1"],
            "entities": [],
        }))

        with (
            patch("core.epilogue.TRANSCRIPT_DIR", tmp_path),
            patch("core.epilogue.request_digest_approval", new_callable=AsyncMock, return_value=True),
            patch("core.epilogue.archive_transcript"),
        ):
            result = await process_session_epilogue(
                "session-3", mock_db, "http://localhost:8420", mock_gateway, 123
            )
        # Should proceed to completion since pending allows retry
        assert result == "completed"

    @pytest.mark.asyncio
    async def test_digest_sent_session_can_be_retried(self, tmp_path):
        """Sessions with epilogue_status='digest_sent' should be retried (HITL timeout)."""
        transcript = tmp_path / "claude-digest-sent.jsonl"
        lines = []
        for i in range(5):
            lines.append(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"Architecture discussion {i}"},
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
                    return_value={"epilogue_status": "digest_sent", "claude_sid": "claude-digest-sent"}
                )
            return cursor

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        mock_gateway = AsyncMock()
        mock_gateway.query = AsyncMock(return_value=json.dumps({
            "digest": "Retry digest after timeout",
            "topics": ["topic1"],
            "entities": [],
        }))

        with (
            patch("core.epilogue.TRANSCRIPT_DIR", tmp_path),
            patch("core.epilogue.request_digest_approval", new_callable=AsyncMock, return_value=False),
            patch("core.epilogue.archive_transcript"),
        ):
            result = await process_session_epilogue(
                "session-4", mock_db, "http://localhost:8420", mock_gateway, 123
            )
        # Should proceed (retry) and end up skipped since denied
        assert result == "skipped"
