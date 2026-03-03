"""API tests for the /epilogue/sweep endpoint."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

_AUTH_HEADER = {"X-HITL-Internal": "test-token"}


class TestEpilogueSweepEndpoint:
    """Tests for POST /epilogue/sweep."""

    def test_epilogue_sweep_endpoint_returns_200(self):
        # Patch session_mgr and config before importing the app
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_mgr.sweep_epilogues = AsyncMock(
                return_value={"processed": 2, "skipped": 1, "errors": 0}
            )
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/epilogue/sweep", headers=_AUTH_HEADER)
            assert response.status_code == 200
            data = response.json()
            assert "processed" in data
            assert "skipped" in data
            assert "errors" in data

    def test_epilogue_sweep_processes_dead_sessions(self):
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_mgr.sweep_epilogues = AsyncMock(
                return_value={"processed": 3, "skipped": 0, "errors": 0}
            )
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/epilogue/sweep", headers=_AUTH_HEADER)
            data = response.json()
            assert data["processed"] == 3
            mock_mgr.sweep_epilogues.assert_called_once()

    def test_epilogue_sweep_rejects_missing_token(self):
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_mgr.sweep_epilogues = AsyncMock(return_value={})
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/epilogue/sweep")
            assert response.status_code == 401

    def test_epilogue_sweep_rejects_wrong_token(self):
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_mgr.sweep_epilogues = AsyncMock(return_value={})
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/epilogue/sweep", headers={"X-HITL-Internal": "wrong"}
            )
            assert response.status_code == 401
