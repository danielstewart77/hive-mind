"""Integration tests for the full epilogue processing flow."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.epilogue import process_session_epilogue


def _make_transcript(tmp_path: Path, claude_sid: str, num_turns: int = 5) -> Path:
    """Helper to create a substantive transcript file."""
    transcript = tmp_path / f"{claude_sid}.jsonl"
    lines = []
    for i in range(num_turns):
        lines.append(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": f"Discussing architecture topic {i} with detailed context"},
            "timestamp": f"2026-03-02T10:0{i}:00Z",
        }))
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": f"Detailed response about architecture topic {i}"}
            ]},
            "timestamp": f"2026-03-02T10:0{i}:30Z",
        }))
    transcript.write_text("\n".join(lines) + "\n")
    return transcript


def _make_mock_db(claude_sid: str, epilogue_status=None):
    """Helper to create a mock database connection."""
    mock_db = AsyncMock()

    async def mock_execute(query, params=None):
        cursor = AsyncMock()
        if "SELECT" in query:
            cursor.fetchone = AsyncMock(
                return_value={"epilogue_status": epilogue_status, "claude_sid": claude_sid}
            )
        return cursor

    mock_db.execute = mock_execute
    mock_db.commit = AsyncMock()
    return mock_db


class TestFullEpilogueFlowApproved:
    """Integration test: full epilogue flow with HITL approval."""

    @pytest.mark.asyncio
    async def test_full_epilogue_flow_approved(self, tmp_path):
        claude_sid = "int-test-sid-approved"
        _make_transcript(tmp_path, claude_sid)

        mock_db = _make_mock_db(claude_sid)
        mock_gateway = AsyncMock()
        mock_gateway.query = AsyncMock(return_value=json.dumps({
            "digest": "Session covered architecture decisions for payment service.",
            "topics": [
                "Payment service architecture: decided on microservices with event sourcing",
                "CQRS implementation: will use separate read/write models",
            ],
            "entities": [
                {"name": "Daniel", "type": "person", "context": "project lead"},
                {"name": "PaymentService", "type": "project", "context": "new microservice"},
            ],
        }))

        with (
            patch("core.epilogue.TRANSCRIPT_DIR", tmp_path),
            patch("core.epilogue.request_digest_approval", new_callable=AsyncMock, return_value=True),
            patch("core.epilogue.archive_transcript") as mock_archive,
        ):
            result = await process_session_epilogue(
                "session-approved", mock_db, "http://localhost:8420", mock_gateway, 123
            )

        assert result == "completed"
        # Verify gateway was called for digest generation
        assert mock_gateway.query.call_count >= 1
        # Verify archive was called
        mock_archive.assert_called_once()


class TestFullEpilogueFlowDenied:
    """Integration test: full epilogue flow with HITL denial."""

    @pytest.mark.asyncio
    async def test_full_epilogue_flow_denied(self, tmp_path):
        claude_sid = "int-test-sid-denied"
        _make_transcript(tmp_path, claude_sid)

        mock_db = _make_mock_db(claude_sid)
        mock_gateway = AsyncMock()
        mock_gateway.query = AsyncMock(return_value=json.dumps({
            "digest": "Session covered weather queries.",
            "topics": ["General queries"],
            "entities": [],
        }))

        with (
            patch("core.epilogue.TRANSCRIPT_DIR", tmp_path),
            patch("core.epilogue.request_digest_approval", new_callable=AsyncMock, return_value=False),
            patch("core.epilogue.archive_transcript") as mock_archive,
            patch("core.epilogue.write_to_memory", new_callable=AsyncMock) as mock_write,
        ):
            result = await process_session_epilogue(
                "session-denied", mock_db, "http://localhost:8420", mock_gateway, 123
            )

        assert result == "skipped"
        # Verify write_to_memory was NOT called
        mock_write.assert_not_called()
        # Archive is still called even on denial
        mock_archive.assert_called_once()
