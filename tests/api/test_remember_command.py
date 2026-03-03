"""API tests for the /remember slash command."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


class TestRememberCommand:
    """Tests for /remember slash command via /command endpoint."""

    def test_remember_command_in_server_commands_set(self):
        from server import SERVER_COMMANDS
        assert "/remember" in SERVER_COMMANDS

    def test_remember_command_triggers_epilogue_on_active_session(self):
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.get_active_session = AsyncMock(return_value={
                "id": "active-session-123",
                "status": "running",
            })
            mock_mgr.force_epilogue = AsyncMock(return_value={
                "status": "completed",
                "session_id": "active-session-123",
            })

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/command", json={
                "content": "/remember",
                "owner_type": "telegram",
                "owner_ref": "user-1",
                "client_ref": "chat-1",
            })
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "completed"
            mock_mgr.force_epilogue.assert_called_once_with("active-session-123")

    def test_remember_command_returns_error_when_no_active_session(self):
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.get_active_session = AsyncMock(return_value=None)

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/command", json={
                "content": "/remember",
                "owner_type": "telegram",
                "owner_ref": "user-1",
                "client_ref": "chat-1",
            })
            assert response.status_code == 200
            data = response.json()
            assert "error" in data
