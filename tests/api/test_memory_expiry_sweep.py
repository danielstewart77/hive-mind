"""API tests for the /memory/expiry-sweep endpoint."""

from unittest.mock import AsyncMock, patch, MagicMock
import asyncio

import pytest
from fastapi.testclient import TestClient

_AUTH_HEADER = {"X-HITL-Internal": "test-token"}


class TestMemoryExpirySweepEndpoint:
    """Tests for POST /memory/expiry-sweep."""

    def test_expiry_sweep_endpoint_returns_200(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg, \
             patch("core.memory_expiry.sweep_expired_events", return_value={"deleted": 1, "prompted": 0, "errors": 0}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/expiry-sweep", headers=_AUTH_HEADER)
            assert response.status_code == 200

    def test_expiry_sweep_rejects_missing_token(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/expiry-sweep")
            assert response.status_code == 401

    def test_expiry_sweep_rejects_wrong_token(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/expiry-sweep",
                headers={"X-HITL-Internal": "wrong-token"},
            )
            assert response.status_code == 401

    def test_expiry_sweep_returns_counts(self) -> None:
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg, \
             patch("core.memory_expiry.sweep_expired_events", return_value={"deleted": 3, "prompted": 1, "errors": 0}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/expiry-sweep", headers=_AUTH_HEADER)
            data = response.json()
            assert "deleted" in data
            assert "prompted" in data
            assert "errors" in data
            assert data["deleted"] == 3
            assert data["prompted"] == 1
            assert data["errors"] == 0

    def test_expiry_sweep_uses_to_thread(self) -> None:
        """Assert that the endpoint runs sweep_expired_events via asyncio.to_thread to avoid blocking."""
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.config") as mock_cfg, \
             patch("core.memory_expiry.sweep_expired_events", return_value={"deleted": 0, "prompted": 0, "errors": 0}) as mock_sweep, \
             patch("server.asyncio") as mock_asyncio:
            mock_cfg.hitl_internal_token = "test-token"

            # Make to_thread return a coroutine that returns the sweep result
            async def fake_to_thread(fn):
                return fn()
            mock_asyncio.to_thread = AsyncMock(side_effect=fake_to_thread)

            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/expiry-sweep", headers=_AUTH_HEADER)
            assert response.status_code == 200
            mock_asyncio.to_thread.assert_called_once_with(mock_sweep)
