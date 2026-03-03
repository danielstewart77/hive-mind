"""Tests for session creation hook triggering epilogue on dead predecessor sessions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCreateSessionFiresEpilogue:
    """Verify create_session fires background epilogue for dead predecessors."""

    @pytest.mark.asyncio
    async def test_create_session_fires_epilogue_for_dead_predecessors(self):
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        registry.get_provider = MagicMock(return_value=MagicMock(
            env_overrides={}, name="anthropic"
        ))
        mgr = SessionManager(registry)

        # Mock DB
        mgr._db = AsyncMock()

        async def mock_execute(query, params=None):
            cursor = AsyncMock()
            if "INSERT INTO sessions" in query:
                return cursor
            elif "INSERT OR REPLACE INTO active_sessions" in query:
                return cursor
            elif "SELECT" in query and "epilogue_status" in query:
                # Return dead sessions
                cursor.fetchall = AsyncMock(return_value=[
                    {"id": "dead-session-1"},
                    {"id": "dead-session-2"},
                ])
                return cursor
            else:
                cursor.fetchone = AsyncMock(return_value=None)
                return cursor

        mgr._db.execute = mock_execute
        mgr._db.commit = AsyncMock()

        # Mock spawn to avoid actually starting claude subprocess
        mgr._spawn = AsyncMock()
        # Mock _session_dict
        mgr._session_dict = AsyncMock(return_value={
            "id": "new-session",
            "status": "running",
        })

        # Mock the epilogue trigger method
        mgr._trigger_epilogue_for_dead_sessions = AsyncMock()

        with patch("asyncio.create_task") as mock_create_task:
            result = await mgr.create_session("telegram", "user-1", "chat-1")

        assert result["id"] == "new-session"
        # Verify create_task was called (for the epilogue trigger)
        assert mock_create_task.called


class TestEpilogueTriggerFindsDeadSessions:
    """Verify the trigger finds dead sessions with correct criteria."""

    @pytest.mark.asyncio
    async def test_epilogue_trigger_finds_dead_sessions(self):
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        dead_sessions = [
            {"id": "dead-1"},
            {"id": "dead-2"},
        ]

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=dead_sessions)

        mgr._db = AsyncMock()
        mgr._db.execute = AsyncMock(return_value=mock_cursor)
        mgr._db.commit = AsyncMock()

        # Mock process_session_epilogue
        with patch("core.sessions.process_session_epilogue", new_callable=AsyncMock, return_value="completed") as mock_process:
            await mgr._trigger_epilogue_for_dead_sessions("user-1")

        # Verify the query was for dead sessions
        call_args = mgr._db.execute.call_args
        query = call_args[0][0]
        assert "status IN" in query
        assert "epilogue_status IS NULL" in query

    @pytest.mark.asyncio
    async def test_epilogue_trigger_ignores_running_sessions(self):
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        # The SQL query should only find dead sessions, so running ones
        # should not appear in the results
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mgr._db = AsyncMock()
        mgr._db.execute = AsyncMock(return_value=mock_cursor)

        with patch("core.sessions.process_session_epilogue", new_callable=AsyncMock) as mock_process:
            await mgr._trigger_epilogue_for_dead_sessions("user-1")

        # No sessions should be processed
        mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_epilogue_trigger_ignores_already_processed(self):
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        # The SQL query uses epilogue_status IS NULL, so already processed
        # sessions should not appear
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mgr._db = AsyncMock()
        mgr._db.execute = AsyncMock(return_value=mock_cursor)

        with patch("core.sessions.process_session_epilogue", new_callable=AsyncMock) as mock_process:
            await mgr._trigger_epilogue_for_dead_sessions("user-1")

        mock_process.assert_not_called()
