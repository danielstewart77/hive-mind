"""Unit tests for POST /sessions/{id}/interrupt on mind_server.py.

Covers: SIGINT delivery to running subprocess, 404 for unknown sessions,
and graceful handling when process has already exited.
"""

import signal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _mock_mind_env(monkeypatch):
    """Set up minimal environment for mind_server import."""
    monkeypatch.setenv("MIND_ID", "ada")


@pytest.fixture()
def client(_mock_mind_env):
    """Create a TestClient for mind_server with a mocked implementation."""
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        # Patch _setup_config_dir to avoid filesystem side effects
        with patch("mind_server._setup_config_dir"):
            import importlib
            import mind_server
            importlib.reload(mind_server)
            yield TestClient(mind_server.app, raise_server_exceptions=False), mind_server


class TestInterruptEndpoint:
    """POST /sessions/{session_id}/interrupt tests."""

    def test_interrupt_sends_sigint_to_running_process(self, client):
        """Calling interrupt on a session with a running process sends SIGINT and returns 200."""
        test_client, mind_server = client
        mock_proc = MagicMock()
        mock_proc.returncode = None  # process is running
        mind_server._sessions["sess-1"] = {"proc": mock_proc, "model": "sonnet"}

        response = test_client.post("/sessions/sess-1/interrupt")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["session_id"] == "sess-1"
        mock_proc.send_signal.assert_called_once_with(signal.SIGINT)

        # Cleanup
        mind_server._sessions.pop("sess-1", None)

    def test_interrupt_session_not_found_returns_404(self, client):
        """Calling interrupt with a nonexistent session_id returns 404."""
        test_client, mind_server = client

        response = test_client.post("/sessions/nonexistent/interrupt")

        assert response.status_code == 404
        data = response.json()
        assert "error" in data

    def test_interrupt_process_not_running_returns_ok_with_message(self, client):
        """Calling interrupt when the process has already exited returns 200 with nothing_running."""
        test_client, mind_server = client
        mock_proc = MagicMock()
        mock_proc.returncode = 0  # process already exited
        mind_server._sessions["sess-2"] = {"proc": mock_proc, "model": "sonnet"}

        response = test_client.post("/sessions/sess-2/interrupt")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["session_id"] == "sess-2"
        assert data["message"] == "nothing_running"
        mock_proc.send_signal.assert_not_called()

        # Cleanup
        mind_server._sessions.pop("sess-2", None)
