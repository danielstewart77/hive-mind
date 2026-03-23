"""API tests for the /remember slash command."""

from unittest.mock import patch

from fastapi.testclient import TestClient


class TestRememberCommand:
    """Tests for /remember slash command via /command endpoint."""

    def test_remember_command_in_server_commands_set(self):
        from server import SERVER_COMMANDS
        assert "/remember" in SERVER_COMMANDS

    def test_remember_command_returns_guidance_message(self):
        with patch("server.session_mgr"):
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
            assert "response" in data
            assert "/new" in data["response"]
