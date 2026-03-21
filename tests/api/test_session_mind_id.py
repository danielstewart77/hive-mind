"""API tests for mind_id support in the session creation endpoint.

Covers: default mind_id, explicit mind_id passthrough, and response inclusion.
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class TestSessionMindIdApi:
    """API-level tests for mind_id in POST /sessions."""

    def test_create_session_without_mind_id_defaults_to_ada(self) -> None:
        """POST /sessions with no mind_id should call create_session with mind_id='ada'."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.create_session = AsyncMock(return_value={
                "id": "sess-1",
                "claude_sid": None,
                "owner_type": "test",
                "owner_ref": "user-1",
                "summary": "New session",
                "model": "sonnet",
                "autopilot": False,
                "created_at": 1000.0,
                "last_active": 1000.0,
                "status": "running",
                "epilogue_status": None,
                "mind_id": "ada",
            })

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions", json={
                "owner_type": "test",
                "owner_ref": "user-1",
                "client_ref": "client-1",
            })

            assert response.status_code == 200
            mock_mgr.create_session.assert_called_once()
            call_kwargs = mock_mgr.create_session.call_args.kwargs
            assert call_kwargs.get("mind_id") == "ada"

    def test_create_session_with_mind_id_passes_through(self) -> None:
        """POST /sessions with mind_id='nagatha' should pass it to session_mgr."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.create_session = AsyncMock(return_value={
                "id": "sess-2",
                "claude_sid": None,
                "owner_type": "test",
                "owner_ref": "user-1",
                "summary": "New session",
                "model": "sonnet",
                "autopilot": False,
                "created_at": 1000.0,
                "last_active": 1000.0,
                "status": "running",
                "epilogue_status": None,
                "mind_id": "nagatha",
            })

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions", json={
                "owner_type": "test",
                "owner_ref": "user-1",
                "client_ref": "client-1",
                "mind_id": "nagatha",
            })

            assert response.status_code == 200
            call_kwargs = mock_mgr.create_session.call_args.kwargs
            assert call_kwargs.get("mind_id") == "nagatha"

    def test_create_session_response_includes_mind_id(self) -> None:
        """POST /sessions response JSON should include mind_id."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.create_session = AsyncMock(return_value={
                "id": "sess-3",
                "claude_sid": None,
                "owner_type": "test",
                "owner_ref": "user-1",
                "summary": "New session",
                "model": "sonnet",
                "autopilot": False,
                "created_at": 1000.0,
                "last_active": 1000.0,
                "status": "running",
                "epilogue_status": None,
                "mind_id": "ada",
            })

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions", json={
                "owner_type": "test",
                "owner_ref": "user-1",
                "client_ref": "client-1",
                "mind_id": "ada",
            })

            assert response.status_code == 200
            data = response.json()
            assert "mind_id" in data
            assert data["mind_id"] == "ada"
