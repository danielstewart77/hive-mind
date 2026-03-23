"""Integration tests for group session DB operations."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
async def tmp_db_path():
    """Create a temp DB path and clean up after."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture
async def session_mgr(tmp_db_path):
    """Create a SessionManager with a temp database."""
    from core.models import ModelRegistry
    from core.sessions import SessionManager

    registry = MagicMock(spec=ModelRegistry)
    mgr = SessionManager(registry)

    with patch.dict(os.environ, {"SESSIONS_DB_PATH": tmp_db_path}):
        await mgr.start()

    yield mgr

    # Cancel background tasks before shutdown
    if mgr._reaper_task:
        mgr._reaper_task.cancel()
    if mgr._guard_task:
        mgr._guard_task.cancel()
    if mgr._db:
        await mgr._db.close()


class TestGroupSessionDB:
    """Integration tests for group session database operations."""

    @pytest.mark.asyncio
    async def test_group_sessions_table_created_on_start(self, session_mgr):
        row = await session_mgr._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='group_sessions'"
        )
        result = await row.fetchone()
        assert result is not None

    @pytest.mark.asyncio
    async def test_group_session_id_column_exists(self, session_mgr):
        """Verify group_session_id column exists in sessions table."""
        row = await session_mgr._db.execute("PRAGMA table_info(sessions)")
        columns = [r[1] for r in await row.fetchall()]
        assert "group_session_id" in columns

    @pytest.mark.asyncio
    async def test_create_group_session_stores_in_db(self, session_mgr):
        result = await session_mgr.create_group_session("ada")
        assert "id" in result
        assert result["moderator_mind_id"] == "ada"

        # Verify it's in the DB
        row = await session_mgr._db.execute(
            "SELECT * FROM group_sessions WHERE id = ?", (result["id"],)
        )
        db_row = await row.fetchone()
        assert db_row is not None

    @pytest.mark.asyncio
    async def test_get_group_session_returns_data(self, session_mgr):
        created = await session_mgr.create_group_session("ada")
        fetched = await session_mgr.get_group_session(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["moderator_mind_id"] == "ada"

    @pytest.mark.asyncio
    async def test_delete_group_session_sets_ended_at(self, session_mgr):
        created = await session_mgr.create_group_session("ada")
        deleted = await session_mgr.delete_group_session(created["id"])
        assert deleted["ended_at"] is not None


class TestGetOrCreateGroupChildSession:
    """Integration tests for SessionManager.get_or_create_group_child_session (M2 fix)."""

    @pytest.mark.asyncio
    async def test_creates_child_session_when_none_exists(self, session_mgr):
        """When no child session exists, creates one and links it to the group."""
        group = await session_mgr.create_group_session("ada")

        # Mock _spawn to avoid actually spawning a process
        with patch.object(session_mgr, "_spawn", new_callable=AsyncMock):
            child_id = await session_mgr.get_or_create_group_child_session(
                group["id"], "ada"
            )

        assert child_id is not None

        # Verify child session is linked to the group
        row = await session_mgr._db.execute(
            "SELECT group_session_id, mind_id FROM sessions WHERE id = ?",
            (child_id,),
        )
        result = await row.fetchone()
        assert result is not None
        assert result["group_session_id"] == group["id"]
        assert result["mind_id"] == "ada"

    @pytest.mark.asyncio
    async def test_returns_existing_child_session(self, session_mgr):
        """When a child session already exists, returns its ID without creating a new one."""
        group = await session_mgr.create_group_session("ada")

        with patch.object(session_mgr, "_spawn", new_callable=AsyncMock):
            first_id = await session_mgr.get_or_create_group_child_session(
                group["id"], "ada"
            )
            second_id = await session_mgr.get_or_create_group_child_session(
                group["id"], "ada"
            )

        assert first_id == second_id

    @pytest.mark.asyncio
    async def test_creates_separate_sessions_for_different_minds(self, session_mgr):
        """Different mind_ids get separate child sessions in the same group."""
        group = await session_mgr.create_group_session("ada")

        with patch.object(session_mgr, "_spawn", new_callable=AsyncMock):
            ada_id = await session_mgr.get_or_create_group_child_session(
                group["id"], "ada"
            )
            nagatha_id = await session_mgr.get_or_create_group_child_session(
                group["id"], "nagatha"
            )

        assert ada_id != nagatha_id
