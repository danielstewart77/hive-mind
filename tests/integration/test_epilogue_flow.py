"""Integration tests for the full epilogue processing flow."""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    claude_sid    TEXT,
    owner_type    TEXT NOT NULL,
    owner_ref     TEXT NOT NULL,
    summary       TEXT DEFAULT 'New session',
    model         TEXT,
    autopilot     INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_active   REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    epilogue_status TEXT DEFAULT NULL,
    mind_id       TEXT DEFAULT 'ada',
    group_session_id TEXT
);

CREATE TABLE IF NOT EXISTS active_sessions (
    client_type   TEXT NOT NULL,
    client_ref    TEXT NOT NULL,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    PRIMARY KEY (client_type, client_ref)
);

CREATE TABLE IF NOT EXISTS group_sessions (
    id                TEXT PRIMARY KEY,
    moderator_mind_id TEXT NOT NULL DEFAULT 'ada',
    created_at        REAL NOT NULL,
    ended_at          REAL
);
"""


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


async def _setup_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA)
    await db.commit()
    return db


class TestAutoWrite:
    """Integration: any session auto-writes memories."""

    @patch("core.epilogue._graph_upsert_direct")
    @patch("core.epilogue._memory_store_direct")
    async def test_any_session_auto_writes(self, mock_mem, mock_graph, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=50, duration_minutes=120.0)

        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        with patch("core.sessions.config", MagicMock()):
            mgr = SessionManager(registry)
        mgr._db = await _setup_db()

        # Insert a session
        await mgr._db.execute(
            """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status)
               VALUES ('sess-1', 'test', 'owner', 'sonnet', 1000.0, 1000.0, 'idle')""",
        )
        await mgr._db.commit()

        # Mock get_transcript_path to return our file
        mgr.get_transcript_path = AsyncMock(return_value=transcript_path)  # type: ignore[method-assign]

        mock_mem.return_value = json.dumps({"ok": True})
        mock_graph.return_value = json.dumps({"ok": True})

        from core.epilogue import process_session
        result = await process_session(
            {"id": "sess-1", "summary": "Test", "mind_id": "ada"},
            mgr,
        )

        assert result["status"] == "done"
        assert result["write_mode"] == "auto"

        # Check epilogue_status was updated
        row = await mgr._db.execute(
            "SELECT epilogue_status FROM sessions WHERE id = 'sess-1'"
        )
        r = await row.fetchone()
        assert r is not None
        assert r["epilogue_status"] == "done"

        await mgr._db.close()


class TestTranscriptDeletion:
    """Integration: transcript is deleted after processing."""

    @patch("core.epilogue._graph_upsert_direct")
    @patch("core.epilogue._memory_store_direct")
    async def test_transcript_deleted_after_processing(self, mock_mem, mock_graph, tmp_path: Path) -> None:
        transcript_path = tmp_path / "real_transcript.jsonl"
        _write_transcript(transcript_path, turns=3)

        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        assert transcript_path.exists()

        from core.epilogue import process_session
        await process_session(
            {"id": "sess-3", "summary": "Short session", "mind_id": "ada"},
            session_mgr,
        )

        assert not transcript_path.exists()


class TestSweepEndpointIntegration:
    """Integration: /epilogue/sweep processes pending sessions."""

    def test_epilogue_sweep_endpoint_processes_pending(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.epilogue.process_pending_sessions", new_callable=AsyncMock) as mock_sweep:
            mock_cfg.hitl_internal_token = "test-token"
            mock_sweep.return_value = {
                "processed": 1, "auto_written": 1, "skipped": 0, "errors": 0, "exceptions": 0,
            }

            from fastapi.testclient import TestClient
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/epilogue/sweep",
                headers={"X-HITL-Internal": "test-token"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["processed"] == 1
            mock_sweep.assert_called_once()


class TestExceptionLogging:
    """Integration: exception triggers are logged at WARNING level."""

    @patch("core.epilogue._notify_exception")
    @patch("core.epilogue._graph_upsert_direct")
    @patch("core.epilogue._memory_store_direct")
    async def test_exception_triggers_are_logged(
        self, mock_mem, mock_graph, mock_notify, tmp_path: Path, caplog
    ) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        _write_transcript(transcript_path, turns=3)

        session_mgr = AsyncMock()
        session_mgr.get_transcript_path = AsyncMock(return_value=transcript_path)
        session_mgr.set_epilogue_status = AsyncMock()

        # Make all writes fail to trigger high_error_rate
        mock_mem.return_value = json.dumps({"error": "Neo4j down"})

        from core.epilogue import process_session

        # We need to provide a digest with entities/memories to trigger errors
        # Patch auto_write_digest to return high error count
        with patch("core.epilogue.auto_write_digest") as mock_auto, \
             patch("core.epilogue.check_exceptions") as mock_check:
            from core.epilogue import EpilogueException
            mock_auto.return_value = {"memories_written": 0, "entities_written": 0, "errors": 5}
            mock_check.return_value = [
                EpilogueException(trigger="high_error_rate", detail="5/5 writes failed"),
            ]

            with caplog.at_level(logging.WARNING, logger="core.epilogue"):
                await process_session(
                    {"id": "sess-exc", "summary": "Error session", "mind_id": "ada"},
                    session_mgr,
                )

            assert any(
                "high_error_rate" in r.message and r.levelno == logging.WARNING
                for r in caplog.records
            )
