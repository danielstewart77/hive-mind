"""API tests for Remote Control endpoints.

Covers: POST /sessions/{id}/remote-control, DELETE /sessions/{id}/remote-control.
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class TestStartRemoteControl:
    """POST /sessions/{id}/remote-control endpoint tests."""

    def test_remote_control_returns_url_and_pid(self):
        """POST /sessions/{id}/remote-control returns 200 with url, session_id, rc_pid."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.spawn_rc_process = AsyncMock(return_value={
                "url": "https://claude.ai/code/sessions/abc123",
                "session_id": "sess-1",
                "rc_pid": 12345,
            })

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions/sess-1/remote-control")

            assert response.status_code == 200
            data = response.json()
            assert data["url"] == "https://claude.ai/code/sessions/abc123"
            assert data["session_id"] == "sess-1"
            assert data["rc_pid"] == 12345

    def test_remote_control_session_not_found(self):
        """POST /sessions/{id}/remote-control returns 404 when session not found."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.spawn_rc_process = AsyncMock(
                side_effect=LookupError("Session not found: nonexistent")
            )

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions/nonexistent/remote-control")

            assert response.status_code == 404
            data = response.json()
            assert "error" in data

    def test_remote_control_no_claude_sid(self):
        """POST /sessions/{id}/remote-control returns 400 when session has no claude_sid."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.spawn_rc_process = AsyncMock(
                side_effect=ValueError("Session sess-1 has no claude_sid")
            )

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions/sess-1/remote-control")

            assert response.status_code == 400
            data = response.json()
            assert "error" in data
            assert "claude_sid" in data["error"]

    def test_remote_control_already_active(self):
        """Calling the endpoint when RC is already active returns the existing info."""
        with patch("server.session_mgr") as mock_mgr:
            # Second call returns same result (idempotent)
            mock_mgr.spawn_rc_process = AsyncMock(return_value={
                "url": "https://claude.ai/code/sessions/abc123",
                "session_id": "sess-1",
                "rc_pid": 12345,
            })

            from server import app
            client = TestClient(app, raise_server_exceptions=False)

            response1 = client.post("/sessions/sess-1/remote-control")
            response2 = client.post("/sessions/sess-1/remote-control")

            assert response1.status_code == 200
            assert response2.status_code == 200
            assert response1.json()["url"] == response2.json()["url"]

    def test_remote_control_timeout_returns_error(self):
        """POST /sessions/{id}/remote-control returns 504 when URL parsing times out."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.spawn_rc_process = AsyncMock(
                side_effect=RuntimeError("Failed to parse RC URL from stdout within timeout")
            )

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions/sess-1/remote-control")

            assert response.status_code == 504
            data = response.json()
            assert "error" in data


class TestStopRemoteControl:
    """DELETE /sessions/{id}/remote-control endpoint tests."""

    def test_stop_remote_control_returns_200(self):
        """DELETE /sessions/{id}/remote-control returns 200 with ok: true."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.kill_rc_process = AsyncMock()

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.delete("/sessions/sess-1/remote-control")

            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is True
            assert data["session_id"] == "sess-1"

    def test_stop_remote_control_no_rc_running(self):
        """DELETE /sessions/{id}/remote-control is a no-op when no RC is running."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.kill_rc_process = AsyncMock()  # no-op

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.delete("/sessions/sess-1/remote-control")

            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is True
