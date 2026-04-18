"""API tests for POST /sessions/{id}/interrupt endpoint.

Covers: success, session not found, nothing running, mind container unreachable.
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class TestInterruptEndpoint:
    """POST /sessions/{id}/interrupt tests."""

    def test_interrupt_returns_200_on_success(self):
        """POST /sessions/{id}/interrupt returns 200 with ok: True when interrupt succeeds."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.interrupt_session = AsyncMock(
                return_value={"ok": True, "session_id": "sess-1"}
            )

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions/sess-1/interrupt")

            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is True
            assert data["session_id"] == "sess-1"

    def test_interrupt_returns_404_when_session_not_found(self):
        """POST /sessions/{id}/interrupt returns 404 when session_mgr raises LookupError."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.interrupt_session = AsyncMock(
                side_effect=LookupError("Session not found: nonexistent")
            )

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions/nonexistent/interrupt")

            assert response.status_code == 404
            data = response.json()
            assert "error" in data

    def test_interrupt_returns_200_with_nothing_running(self):
        """POST /sessions/{id}/interrupt returns 200 with nothing_running message."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.interrupt_session = AsyncMock(
                return_value={"ok": True, "session_id": "sess-1", "message": "nothing_running"}
            )

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions/sess-1/interrupt")

            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is True
            assert data["message"] == "nothing_running"

    def test_interrupt_returns_502_when_mind_unreachable(self):
        """POST /sessions/{id}/interrupt returns 502 when session_mgr raises RuntimeError."""
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.interrupt_session = AsyncMock(
                side_effect=RuntimeError("Mind container unreachable")
            )

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/sessions/sess-1/interrupt")

            assert response.status_code == 502
            data = response.json()
            assert "error" in data
