"""Unit tests for process_session() function."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from config import EpilogueThresholds
from core.epilogue import process_session


def _make_session(session_id: str = "test-sess", transcript_path: Path | None = None) -> dict:
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

    @patch("core.epilogue.auto_write_digest")
    @patch("core.epilogue.hitl_write_digest")
    async def test_below_threshold_auto_writes(self, mock_hitl_write, mock_auto_write, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=3, duration_minutes=10.0)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        mock_auto_write.return_value = {"memories_written": 0, "entities_written": 0, "errors": 0}

        await process_session(session, session_mgr, EpilogueThresholds())

        mock_auto_write.assert_called_once()
        mock_hitl_write.assert_not_called()

    @patch("core.epilogue.auto_write_digest")
    @patch("core.epilogue.hitl_write_digest")
    async def test_above_threshold_uses_hitl(self, mock_hitl_write, mock_auto_write, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        # 25 turns exceeds default threshold of 20
        _write_transcript(transcript_path, turns=25, duration_minutes=10.0)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        mock_hitl_write.return_value = {"memories_written": 0, "entities_written": 0, "errors": 0}

        await process_session(session, session_mgr, EpilogueThresholds())

        mock_hitl_write.assert_called_once()
        mock_auto_write.assert_not_called()

    @patch("core.epilogue.auto_write_digest")
    async def test_deletes_transcript_after_processing(self, mock_auto_write, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=3)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        mock_auto_write.return_value = {"memories_written": 0, "entities_written": 0, "errors": 0}

        await process_session(session, session_mgr, EpilogueThresholds())

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

        await process_session(session, session_mgr, EpilogueThresholds())

        session_mgr.set_epilogue_status.assert_called_once_with("test-sess", "done")

    async def test_no_transcript_sets_status_skipped(self) -> None:
        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=None)
        session_mgr.set_epilogue_status = AsyncMock()

        await process_session(session, session_mgr, EpilogueThresholds())

        session_mgr.set_epilogue_status.assert_called_once_with("test-sess", "skipped")

    @patch("core.epilogue.auto_write_digest", side_effect=RuntimeError("boom"))
    async def test_error_sets_status_skipped(self, mock_auto_write, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=3)

        session = _make_session()
        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        await process_session(session, session_mgr, EpilogueThresholds())

        session_mgr.set_epilogue_status.assert_called_once_with("test-sess", "skipped")
