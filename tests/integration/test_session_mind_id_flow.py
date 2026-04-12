"""Integration tests for mind_id flow through session manager with real SQLite.

Covers: DB storage of mind_id, default mind_id, get_session response,
and migration of existing databases without mind_id column.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest


@pytest.fixture
def tmp_db_path(tmp_path):
    """Provide a temporary database path."""
    return str(tmp_path / "test_sessions.db")


class TestSessionMindIdFlow:
    """Integration tests for mind_id across the session manager stack."""

    @pytest.mark.asyncio
    async def test_create_session_stores_mind_id_in_db(self, tmp_db_path):
        """create_session(mind_id='ada') should store 'ada' in the DB row."""
        from core.sessions import SessionManager
        from core.models import ModelRegistry, Provider

        registry = MagicMock(spec=ModelRegistry)
        provider = MagicMock(spec=Provider)
        provider.env_overrides = {}
        registry.get_provider = MagicMock(return_value=provider)

        mgr = SessionManager(registry)

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": tmp_db_path}), \
             patch.object(mgr, "_spawn", new_callable=AsyncMock), \
             patch("core.sessions.config") as mock_config:
            mock_config.default_model = "sonnet"
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()
            await mgr.create_session(
                owner_type="test",
                owner_ref="user-1",
                client_ref="client-1",
                mind_id="ada",
            )

            # Query the DB directly to verify mind_id
            async with aiosqlite.connect(tmp_db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT mind_id FROM sessions LIMIT 1")
                row = await cursor.fetchone()
                assert row is not None
                assert row["mind_id"] == "ada"

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_create_session_default_mind_id_in_db(self, tmp_db_path):
        """create_session() without mind_id should default to 'ada' in DB."""
        from core.sessions import SessionManager
        from core.models import ModelRegistry, Provider

        registry = MagicMock(spec=ModelRegistry)
        provider = MagicMock(spec=Provider)
        provider.env_overrides = {}
        registry.get_provider = MagicMock(return_value=provider)

        mgr = SessionManager(registry)

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": tmp_db_path}), \
             patch.object(mgr, "_spawn", new_callable=AsyncMock), \
             patch("core.sessions.config") as mock_config:
            mock_config.default_model = "sonnet"
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()
            await mgr.create_session(
                owner_type="test",
                owner_ref="user-1",
                client_ref="client-1",
            )

            async with aiosqlite.connect(tmp_db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT mind_id FROM sessions LIMIT 1")
                row = await cursor.fetchone()
                assert row is not None
                assert row["mind_id"] == "ada"

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_session_dict_returns_mind_id(self, tmp_db_path):
        """After creating a session, get_session() should include mind_id."""
        from core.sessions import SessionManager
        from core.models import ModelRegistry, Provider

        registry = MagicMock(spec=ModelRegistry)
        provider = MagicMock(spec=Provider)
        provider.env_overrides = {}
        registry.get_provider = MagicMock(return_value=provider)

        mgr = SessionManager(registry)

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": tmp_db_path}), \
             patch.object(mgr, "_spawn", new_callable=AsyncMock), \
             patch("core.sessions.config") as mock_config:
            mock_config.default_model = "sonnet"
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()
            session = await mgr.create_session(
                owner_type="test",
                owner_ref="user-1",
                client_ref="client-1",
                mind_id="ada",
            )

            assert "mind_id" in session
            assert session["mind_id"] == "ada"

            # Also verify via get_session
            fetched = await mgr.get_session(session["id"])
            assert fetched is not None
            assert fetched["mind_id"] == "ada"

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_migration_adds_mind_id_to_existing_db(self, tmp_db_path):
        """Calling start() on a DB without the mind_id column should add it via migration."""
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        # Create a DB with the old schema (no mind_id column)
        old_schema = """
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
            epilogue_status TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS active_sessions (
            client_type   TEXT NOT NULL,
            client_ref    TEXT NOT NULL,
            session_id    TEXT NOT NULL REFERENCES sessions(id),
            PRIMARY KEY (client_type, client_ref)
        );
        """
        async with aiosqlite.connect(tmp_db_path) as db:
            await db.executescript(old_schema)
            # Insert a row without mind_id
            await db.execute(
                """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status)
                   VALUES ('old-session-1', 'terminal', 'user-0', 'sonnet', 100.0, 100.0, 'idle')"""
            )
            await db.commit()

        # Now start the session manager, which should migrate
        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": tmp_db_path}), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()

            # Verify the column exists and has the default value
            async with aiosqlite.connect(tmp_db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT mind_id FROM sessions WHERE id = 'old-session-1'")
                row = await cursor.fetchone()
                assert row is not None
                assert row["mind_id"] == "ada"

            await mgr.shutdown()
