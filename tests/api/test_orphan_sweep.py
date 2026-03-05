"""API tests for the /memory/orphan-sweep endpoint."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi.testclient import TestClient

_AUTH_HEADER = {"X-HITL-Internal": "test-token"}


class TestOrphanSweepEndpoint:
    """Tests for POST /memory/orphan-sweep."""

    def test_orphan_sweep_endpoint_returns_200(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg, \
             patch("core.orphan_sweep.sweep_orphan_nodes", return_value={"orphans_found": 1, "notified": True, "errors": 0}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/orphan-sweep", headers=_AUTH_HEADER)
            assert response.status_code == 200

    def test_orphan_sweep_rejects_missing_token(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/orphan-sweep")
            assert response.status_code == 401

    def test_orphan_sweep_rejects_wrong_token(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/orphan-sweep",
                headers={"X-HITL-Internal": "wrong-token"},
            )
            assert response.status_code == 401

    def test_orphan_sweep_returns_result_counts(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg, \
             patch("core.orphan_sweep.sweep_orphan_nodes", return_value={"orphans_found": 3, "notified": True, "errors": 0}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/orphan-sweep", headers=_AUTH_HEADER)
            data = response.json()
            assert "orphans_found" in data
            assert "notified" in data
            assert "errors" in data
            assert data["orphans_found"] == 3
            assert data["notified"] is True
            assert data["errors"] == 0
