"""API tests for group session endpoints."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class TestGroupSessionApi:
    """API-level tests for group session endpoints."""

    def test_create_group_session_returns_200(self):
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.create_group_session = AsyncMock(return_value={
                "id": "group-1",
                "moderator_mind_id": "ada",
                "created_at": 1000.0,
                "ended_at": None,
            })

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/group-sessions", json={"moderator_mind_id": "ada"})

            assert response.status_code == 200
            data = response.json()
            assert "id" in data
            assert data["moderator_mind_id"] == "ada"

    def test_create_group_session_defaults_moderator_to_ada(self):
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.create_group_session = AsyncMock(return_value={
                "id": "group-2",
                "moderator_mind_id": "ada",
                "created_at": 1000.0,
                "ended_at": None,
            })

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/group-sessions", json={})

            assert response.status_code == 200
            mock_mgr.create_group_session.assert_called_once_with("ada")

    def test_get_group_session_returns_data(self):
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.get_group_session = AsyncMock(return_value={
                "id": "group-3",
                "moderator_mind_id": "ada",
                "created_at": 1000.0,
                "ended_at": None,
            })
            mock_mgr.get_group_transcript = AsyncMock(return_value=[])

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/group-sessions/group-3")

            assert response.status_code == 200
            data = response.json()
            assert data["id"] == "group-3"

    def test_get_group_session_not_found(self):
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.get_group_session = AsyncMock(return_value=None)

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/group-sessions/nonexistent")

            assert response.status_code == 404

    def test_delete_group_session(self):
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.delete_group_session = AsyncMock(return_value={
                "id": "group-4",
                "moderator_mind_id": "ada",
                "created_at": 1000.0,
                "ended_at": 2000.0,
            })

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.delete("/group-sessions/group-4")

            assert response.status_code == 200
            data = response.json()
            assert data["ended_at"] is not None

    def test_send_group_message_uses_public_method(self):
        """M2: send_group_message must use get_or_create_group_child_session, not _db."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.get_group_session = AsyncMock(return_value={
                "id": "group-msg-1",
                "moderator_mind_id": "ada",
                "created_at": 1000.0,
                "ended_at": None,
            })
            mock_mgr.get_or_create_group_child_session = AsyncMock(
                return_value="child-session-1"
            )

            async def mock_send_message(session_id, content, **kwargs):
                yield {"type": "result", "result": "ok", "session_id": None}

            mock_mgr.send_message = mock_send_message

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/group-sessions/group-msg-1/message",
                json={"content": "Hello group"},
            )

            assert response.status_code == 200
            mock_mgr.get_or_create_group_child_session.assert_called_once_with(
                "group-msg-1", "ada"
            )
