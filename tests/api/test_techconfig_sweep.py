"""API tests for the /memory/techconfig-sweep endpoint."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi.testclient import TestClient

_AUTH_HEADER = {"X-HITL-Internal": "test-token"}


class TestTechconfigSweepEndpoint:
    """Tests for POST /memory/techconfig-sweep."""

    def test_techconfig_sweep_endpoint_returns_200(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg, \
             patch("core.techconfig_pruning.sweep_techconfig_entries", return_value={"verified": 1, "pruned": 0, "flagged": 0, "errors": 0}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/techconfig-sweep", headers=_AUTH_HEADER)
            assert response.status_code == 200

    def test_techconfig_sweep_rejects_missing_token(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/techconfig-sweep")
            assert response.status_code == 401

    def test_techconfig_sweep_rejects_wrong_token(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/techconfig-sweep",
                headers={"X-HITL-Internal": "wrong-token"},
            )
            assert response.status_code == 401

    def test_techconfig_sweep_returns_result_counts(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg, \
             patch("core.techconfig_pruning.sweep_techconfig_entries", return_value={"verified": 5, "pruned": 2, "flagged": 1, "errors": 0}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/techconfig-sweep", headers=_AUTH_HEADER)
            data = response.json()
            assert "verified" in data
            assert "pruned" in data
            assert "flagged" in data
            assert "errors" in data
            assert data["verified"] == 5
            assert data["pruned"] == 2
            assert data["flagged"] == 1
            assert data["errors"] == 0
