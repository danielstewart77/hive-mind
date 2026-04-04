"""Unit tests for SessionManager epilogue query methods."""

import pytest
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


async def _setup_db():
    """Create an in-memory SQLite DB with the sessions schema."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA)
    await db.commit()
    return db


async def _insert_session(db, session_id: str, status: str, epilogue_status: str | None = None):
    """Insert a test session row."""
    await db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status, epilogue_status)
           VALUES (?, 'test', 'test-owner', 'sonnet', 1000.0, 1000.0, ?, ?)""",
        (session_id, status, epilogue_status),
    )
    await db.commit()


class TestGetSessionsPendingEpilogue:
    """Tests for SessionManager.get_sessions_pending_epilogue()."""

    @pytest.fixture(autouse=True)
    async def setup_manager(self, monkeypatch):
        """Set up a SessionManager with an in-memory DB."""
        from unittest.mock import MagicMock
        monkeypatch.setattr("core.sessions.config", MagicMock())
        from core.sessions import SessionManager
        from core.models import ModelRegistry
        registry = MagicMock(spec=ModelRegistry)
        self.mgr = SessionManager(registry)
        self.mgr._db = await _setup_db()
        yield
        await self.mgr._db.close()

    async def test_returns_idle_sessions(self) -> None:
        await _insert_session(self.mgr._db, "sess-1", "idle", None)
        await _insert_session(self.mgr._db, "sess-2", "idle", None)
        result = await self.mgr.get_sessions_pending_epilogue()
        ids = [r["id"] for r in result]
        assert "sess-1" in ids
        assert "sess-2" in ids

    async def test_excludes_processed(self) -> None:
        await _insert_session(self.mgr._db, "sess-done", "idle", "done")
        result = await self.mgr.get_sessions_pending_epilogue()
        ids = [r["id"] for r in result]
        assert "sess-done" not in ids

    async def test_excludes_running(self) -> None:
        await _insert_session(self.mgr._db, "sess-running", "running", None)
        result = await self.mgr.get_sessions_pending_epilogue()
        ids = [r["id"] for r in result]
        assert "sess-running" not in ids

    async def test_includes_closed(self) -> None:
        await _insert_session(self.mgr._db, "sess-closed", "closed", None)
        result = await self.mgr.get_sessions_pending_epilogue()
        ids = [r["id"] for r in result]
        assert "sess-closed" in ids


class TestSetEpilogueStatus:
    """Tests for SessionManager.set_epilogue_status()."""

    @pytest.fixture(autouse=True)
    async def setup_manager(self, monkeypatch):
        from unittest.mock import MagicMock
        monkeypatch.setattr("core.sessions.config", MagicMock())
        from core.sessions import SessionManager
        from core.models import ModelRegistry
        registry = MagicMock(spec=ModelRegistry)
        self.mgr = SessionManager(registry)
        self.mgr._db = await _setup_db()
        yield
        await self.mgr._db.close()

    async def test_set_epilogue_status_done(self) -> None:
        await _insert_session(self.mgr._db, "sess-1", "idle", None)
        await self.mgr.set_epilogue_status("sess-1", "done")
        row = await self.mgr._db.execute(
            "SELECT epilogue_status FROM sessions WHERE id = ?", ("sess-1",)
        )
        result = await row.fetchone()
        assert result["epilogue_status"] == "done"

    async def test_set_epilogue_status_skipped(self) -> None:
        await _insert_session(self.mgr._db, "sess-2", "idle", None)
        await self.mgr.set_epilogue_status("sess-2", "skipped")
        row = await self.mgr._db.execute(
            "SELECT epilogue_status FROM sessions WHERE id = ?", ("sess-2",)
        )
        result = await row.fetchone()
        assert result["epilogue_status"] == "skipped"
