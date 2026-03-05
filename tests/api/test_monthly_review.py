"""API tests for the /memory/monthly-review and /memory/review-respond endpoints."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

_AUTH_HEADER = {"X-HITL-Internal": "test-token"}


class TestMonthlyReviewEndpoint:
    """Tests for POST /memory/monthly-review."""

    def test_monthly_review_endpoint_returns_200(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.monthly_review.sweep_monthly_review", return_value={"entries_found": 3, "messages_sent": 2, "errors": 0}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/monthly-review", headers=_AUTH_HEADER)
            assert response.status_code == 200

    def test_monthly_review_rejects_missing_token(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/monthly-review")
            assert response.status_code == 401

    def test_monthly_review_rejects_wrong_token(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/monthly-review",
                headers={"X-HITL-Internal": "wrong-token"},
            )
            assert response.status_code == 401

    def test_monthly_review_returns_summary_counts(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.monthly_review.sweep_monthly_review", return_value={"entries_found": 5, "messages_sent": 3, "errors": 0}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/monthly-review", headers=_AUTH_HEADER)
            data = response.json()
            assert "entries_found" in data
            assert "messages_sent" in data
            assert "errors" in data
            assert data["entries_found"] == 5
            assert data["messages_sent"] == 3

    def test_monthly_review_uses_to_thread(self) -> None:
        """Assert that the endpoint runs sweep via asyncio.to_thread."""
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.monthly_review.sweep_monthly_review", return_value={"entries_found": 0, "messages_sent": 0, "errors": 0}) as mock_sweep, \
             patch("server.asyncio") as mock_asyncio:
            mock_cfg.hitl_internal_token = "test-token"

            async def fake_to_thread(fn):
                return fn()
            mock_asyncio.to_thread = AsyncMock(side_effect=fake_to_thread)

            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/memory/monthly-review", headers=_AUTH_HEADER)
            assert response.status_code == 200
            mock_asyncio.to_thread.assert_called_once_with(mock_sweep)


class TestReviewRespondEndpoint:
    """Tests for POST /memory/review-respond."""

    def test_review_respond_keep_returns_200(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.monthly_review.handle_keep", return_value={"ok": True, "action": "keep"}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/review-respond",
                headers=_AUTH_HEADER,
                json={"element_id": "4:abc:123", "action": "keep"},
            )
            assert response.status_code == 200

    def test_review_respond_archive_returns_200(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.monthly_review.handle_archive", return_value={"ok": True, "action": "archive"}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/review-respond",
                headers=_AUTH_HEADER,
                json={"element_id": "4:abc:123", "action": "archive"},
            )
            assert response.status_code == 200

    def test_review_respond_discard_returns_200(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.monthly_review.handle_discard", return_value={"ok": True, "action": "discard"}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/review-respond",
                headers=_AUTH_HEADER,
                json={"element_id": "4:abc:123", "action": "discard"},
            )
            assert response.status_code == 200

    def test_review_respond_invalid_action_returns_400(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/review-respond",
                headers=_AUTH_HEADER,
                json={"element_id": "4:abc:123", "action": "unknown"},
            )
            assert response.status_code == 400

    def test_review_respond_rejects_missing_token(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/review-respond",
                json={"element_id": "4:abc:123", "action": "keep"},
            )
            assert response.status_code == 401

    def test_review_respond_returns_handler_result(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("core.monthly_review.handle_keep", return_value={"ok": True, "action": "keep"}):
            mock_cfg.hitl_internal_token = "test-token"
            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/memory/review-respond",
                headers=_AUTH_HEADER,
                json={"element_id": "4:abc:123", "action": "keep"},
            )
            data = response.json()
            assert data["ok"] is True
            assert data["action"] == "keep"
