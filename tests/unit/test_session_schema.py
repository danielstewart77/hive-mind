"""Tests for session schema changes: epilogue_status column and transcript path resolution."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSchemaIncludesEpilogueStatus:
    """Verify the _SCHEMA string includes epilogue_status column."""

    def test_schema_includes_epilogue_status_column(self):
        from core.sessions import _SCHEMA

        assert "epilogue_status" in _SCHEMA
        assert "epilogue_status TEXT" in _SCHEMA


class TestSessionDictIncludesEpilogueStatus:
    """Verify _session_dict output includes epilogue_status key."""

    @pytest.mark.asyncio
    async def test_session_dict_includes_epilogue_status(self):
        from core.sessions import SessionManager, _SCHEMA
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        # Create a mock row that behaves like an aiosqlite.Row
        mock_row = {
            "id": "test-session-id",
            "claude_sid": "claude-123",
            "owner_type": "telegram",
            "owner_ref": "user-1",
            "summary": "Test session",
            "model": "sonnet",
            "autopilot": 0,
            "created_at": 1000.0,
            "last_active": 1000.0,
            "status": "running",
            "epilogue_status": None,
        }

        # Mock the _get_row method to return our mock row
        mgr._get_row = AsyncMock(return_value=mock_row)

        result = await mgr._session_dict("test-session-id")
        assert result is not None
        assert "epilogue_status" in result
        assert result["epilogue_status"] is None


class TestGetTranscriptPath:
    """Verify transcript path resolution."""

    @pytest.mark.asyncio
    async def test_get_transcript_path_returns_correct_path(self, tmp_path):
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        # Mock the DB to return a claude_sid
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value={"claude_sid": "abc123"})
        mgr._db = AsyncMock()
        mgr._db.execute = AsyncMock(return_value=mock_cursor)

        # Create a temporary transcript file so the file existence check passes
        with patch("core.sessions._TRANSCRIPT_DIR", tmp_path):
            transcript_file = tmp_path / "abc123.jsonl"
            transcript_file.write_text('{"type":"user"}\n')

            result = await mgr.get_transcript_path("test-session-id")
            assert result is not None
            assert result == transcript_file

    @pytest.mark.asyncio
    async def test_get_transcript_path_returns_none_when_no_claude_sid(self):
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        # Mock the DB to return no claude_sid
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value={"claude_sid": None})
        mgr._db = AsyncMock()
        mgr._db.execute = AsyncMock(return_value=mock_cursor)

        result = await mgr.get_transcript_path("test-session-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_transcript_path_returns_none_when_file_missing(self, tmp_path):
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        # Mock the DB to return a claude_sid
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value={"claude_sid": "nonexistent"})
        mgr._db = AsyncMock()
        mgr._db.execute = AsyncMock(return_value=mock_cursor)

        # Patch TRANSCRIPT_DIR to tmp_path where no file exists
        with patch("core.sessions._TRANSCRIPT_DIR", tmp_path):
            result = await mgr.get_transcript_path("test-session-id")
            assert result is None
