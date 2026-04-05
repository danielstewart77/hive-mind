"""API tests for the POST /epilogue/sweep endpoint."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

_AUTH_HEADER = {"X-HITL-Internal": "test-token"}


class TestEpilogueSweepEndpoint:
    """Tests for POST /epilogue/sweep."""

    def test_returns_200_with_valid_auth(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.epilogue.process_pending_sessions", new_callable=AsyncMock, return_value={
                 "processed": 0, "auto_written": 0, "skipped": 0, "errors": 0, "exceptions": 0,
             }):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/epilogue/sweep", headers=_AUTH_HEADER)
            assert response.status_code == 200

    def test_rejects_missing_token(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/epilogue/sweep")
            assert response.status_code == 401

    def test_rejects_wrong_token(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/epilogue/sweep",
                headers={"X-HITL-Internal": "wrong-token"},
            )
            assert response.status_code == 401

    def test_returns_summary_counts(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.epilogue.process_pending_sessions", new_callable=AsyncMock, return_value={
                 "processed": 3, "auto_written": 2, "skipped": 0, "errors": 0, "exceptions": 1,
             }):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/epilogue/sweep", headers=_AUTH_HEADER)
            data = response.json()
            assert data["processed"] == 3
            assert data["auto_written"] == 2
            assert data["skipped"] == 0
            assert data["errors"] == 0
            assert data["exceptions"] == 1

    def test_unconfigured_hitl_returns_500(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = ""
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/epilogue/sweep", headers=_AUTH_HEADER)
            assert response.status_code == 500
