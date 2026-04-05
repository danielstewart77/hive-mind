"""Unit tests for process_session() function."""

import inspect
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.epilogue import process_session


def _make_session(session_id: str = "test-sess") -> dict:
    return {
        "id": session_id,
        "summary": "Test session",
        "model": "sonnet",
        "mind_id": "ada",
        "status": "idle",
        "epilogue_status": None,
    }


def _write_transcript(path: Path, turns: int = 3, duration_minutes: float = 10.0) -> None:
    """Write a minimal JSONL transcript file."""
    lines = []
    for i in range(turns):
        minute_offset = int(i * (duration_minutes / max(turns, 1)))
        lines.append({
            "type": "user",
            "message": {"role": "user", "content": f"Message {i + 1}"},
            "timestamp": f"2026-01-01T10:{minute_offset:02d}:00Z",
        })
        lines.append({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": f"Response {i + 1}"}]},
            "timestamp": f"2026-01-01T10:{minute_offset:02d}:30Z",
        })
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


class TestProcessSession:
    """Tests for process_session() function."""

    @patch("core.epilogue._notify_exception")
    @patch("core.epilogue.auto_write_digest")
    async def test_always_auto_writes(self, mock_auto_write, mock_notify, tmp_path: Path) -> None:
        """Any session calls auto_write_digest(), regardless of turn count."""
        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=50, duration_minutes=120.0)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        mock_auto_write.return_value = {"memories_written": 0, "entities_written": 0, "errors": 0}

        await process_session(session, session_mgr)

        mock_auto_write.assert_called_once()

    @patch("core.epilogue._notify_exception")
    @patch("core.epilogue.check_exceptions")
    @patch("core.epilogue.auto_write_digest")
    async def test_exception_triggers_hitl_notification(
        self, mock_auto_write, mock_check, mock_notify, tmp_path: Path
    ) -> None:
        """When check_exceptions returns exceptions, HITL notification is sent."""
        from core.epilogue import EpilogueException

        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=3)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        mock_auto_write.return_value = {"memories_written": 0, "entities_written": 0, "errors": 0}
        mock_check.return_value = [
            EpilogueException(trigger="high_novel_entities", detail="11 novel entities"),
        ]

        await process_session(session, session_mgr)

        mock_notify.assert_called_once()

    @patch("core.epilogue._notify_exception")
    @patch("core.epilogue.check_exceptions")
    @patch("core.epilogue.auto_write_digest")
    async def test_exception_logged_at_warning(
        self, mock_auto_write, mock_check, mock_notify, tmp_path: Path, caplog
    ) -> None:
        """When exceptions found, a WARNING log is emitted."""
        from core.epilogue import EpilogueException

        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=3)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        mock_auto_write.return_value = {"memories_written": 0, "entities_written": 0, "errors": 0}
        mock_check.return_value = [
            EpilogueException(trigger="high_novel_entities", detail="11 novel entities"),
        ]

        with caplog.at_level(logging.WARNING, logger="core.epilogue"):
            await process_session(session, session_mgr)

        assert any("high_novel_entities" in r.message for r in caplog.records)

    @patch("core.epilogue.auto_write_digest")
    async def test_deletes_transcript_after_processing(self, mock_auto_write, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=3)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        mock_auto_write.return_value = {"memories_written": 0, "entities_written": 0, "errors": 0}

        await process_session(session, session_mgr)

        assert not transcript_path.exists()

    @patch("core.epilogue.auto_write_digest")
    async def test_sets_epilogue_status_done(self, mock_auto_write, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=3)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        mock_auto_write.return_value = {"memories_written": 0, "entities_written": 0, "errors": 0}

        await process_session(session, session_mgr)

        session_mgr.set_epilogue_status.assert_called_once_with("test-sess", "done")

    async def test_no_transcript_sets_status_skipped(self) -> None:
        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=None)
        session_mgr.set_epilogue_status = AsyncMock()

        await process_session(session, session_mgr)

        session_mgr.set_epilogue_status.assert_called_once_with("test-sess", "skipped")

    @patch("core.epilogue.auto_write_digest", side_effect=RuntimeError("boom"))
    async def test_error_sets_status_skipped(self, mock_auto_write, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=3)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        await process_session(session, session_mgr)

        session_mgr.set_epilogue_status.assert_called_once_with("test-sess", "skipped")

    def test_no_thresholds_parameter(self) -> None:
        """process_session() takes only session and session_mgr (no thresholds)."""
        sig = inspect.signature(process_session)
        param_names = list(sig.parameters.keys())
        assert param_names == ["session", "session_mgr"]
