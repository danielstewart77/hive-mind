"""Unit tests for process_pending_sessions() batch sweep function."""

from unittest.mock import AsyncMock, patch

from config import EpilogueThresholds
from core.epilogue import process_pending_sessions


class TestProcessPendingSessions:
    """Tests for process_pending_sessions() function."""

    @patch("core.epilogue.process_session")
    async def test_processes_each_session(self, mock_process) -> None:
        mock_process.return_value = {
            "session_id": "s", "status": "done", "write_mode": "auto",
            "memories_written": 0, "entities_written": 0, "errors": 0,
        }
        session_mgr = AsyncMock()
        session_mgr.get_sessions_pending_epilogue = AsyncMock(return_value=[
            {"id": "s1", "status": "idle"},
            {"id": "s2", "status": "idle"},
            {"id": "s3", "status": "closed"},
        ])

        await process_pending_sessions(session_mgr, EpilogueThresholds())

        assert mock_process.call_count == 3

    @patch("core.epilogue.process_session")
    async def test_returns_summary(self, mock_process) -> None:
        mock_process.side_effect = [
            {"session_id": "s1", "status": "done", "write_mode": "auto", "memories_written": 2, "entities_written": 1, "errors": 0},
            {"session_id": "s2", "status": "done", "write_mode": "hitl", "memories_written": 1, "entities_written": 0, "errors": 0},
            {"session_id": "s3", "status": "skipped", "reason": "no_transcript"},
        ]
        session_mgr = AsyncMock()
        session_mgr.get_sessions_pending_epilogue = AsyncMock(return_value=[
            {"id": "s1"}, {"id": "s2"}, {"id": "s3"},
        ])

        result = await process_pending_sessions(session_mgr, EpilogueThresholds())

        assert result["processed"] == 3
        assert result["auto_written"] == 1
        assert result["hitl_sent"] == 1
        assert result["skipped"] == 1

    @patch("core.epilogue.process_session")
    async def test_no_pending_returns_zeros(self, mock_process) -> None:
        session_mgr = AsyncMock()
        session_mgr.get_sessions_pending_epilogue = AsyncMock(return_value=[])

        result = await process_pending_sessions(session_mgr, EpilogueThresholds())

        assert result["processed"] == 0
        assert result["auto_written"] == 0
        assert result["hitl_sent"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == 0
        mock_process.assert_not_called()

    @patch("core.epilogue.process_session")
    async def test_continues_on_error(self, mock_process) -> None:
        mock_process.side_effect = [
            RuntimeError("boom"),
            {"session_id": "s2", "status": "done", "write_mode": "auto", "memories_written": 0, "entities_written": 0, "errors": 0},
        ]
        session_mgr = AsyncMock()
        session_mgr.get_sessions_pending_epilogue = AsyncMock(return_value=[
            {"id": "s1"}, {"id": "s2"},
        ])

        result = await process_pending_sessions(session_mgr, EpilogueThresholds())

        assert result["processed"] == 2
        assert result["errors"] == 1
        assert mock_process.call_count == 2
