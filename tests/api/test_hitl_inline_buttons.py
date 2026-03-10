"""API tests for the HITL inline keyboard button flow through server.py.

Tests the end-to-end flow: creating HITL requests with inline keyboard,
resolving tokens, and message tracking.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

_AUTH_HEADER = {"X-HITL-Internal": "test-token"}


class TestHitlInlineButtons:
    """API-level tests for HITL inline keyboard buttons."""

    def test_hitl_request_sends_inline_keyboard_via_telegram(self) -> None:
        """POST /hitl/request should trigger a Telegram message with inline keyboard."""
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("server._send_telegram_approval_request", new_callable=AsyncMock) as mock_send, \
             patch("server.hitl_store") as mock_store:
            mock_cfg.hitl_internal_token = "test-token"
            mock_cfg.server_port = 8420

            # Mock create to return a token and a mock entry
            mock_entry = MagicMock()
            mock_entry.event = MagicMock()
            mock_entry.event.wait = AsyncMock()
            mock_entry.approved = None
            mock_store.create.return_value = ("testtoken", mock_entry)

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/hitl/request",
                json={"action": "send_email", "summary": "Test email action", "wait": False},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["token"] == "testtoken"
            assert data["state"] == "pending"

            # Verify _send_telegram_approval_request was called
            mock_send.assert_called_once_with("testtoken", "Test email action")

    def test_hitl_respond_resolves_token(self) -> None:
        """POST /hitl/respond with valid token should return ok=true."""
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("server.hitl_store") as mock_store:
            mock_cfg.hitl_internal_token = "test-token"

            mock_store.resolve.return_value = True

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/hitl/respond",
                json={"token": "abc123", "approved": True},
                headers=_AUTH_HEADER,
            )

            assert response.status_code == 200
            assert response.json() == {"ok": True}

    def test_hitl_respond_rejects_expired_token(self) -> None:
        """POST /hitl/respond with expired token should return 404."""
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("server.hitl_store") as mock_store:
            mock_cfg.hitl_internal_token = "test-token"

            mock_store.resolve.return_value = False

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/hitl/respond",
                json={"token": "expired123", "approved": True},
                headers=_AUTH_HEADER,
            )

            assert response.status_code == 404

    def test_hitl_messages_tracking_populated_after_request(self) -> None:
        """After sending an HITL request, _hitl_messages should contain the token."""
        with patch("server.session_mgr"), \
             patch("server.config") as mock_cfg, \
             patch("server.hitl_store") as mock_store:
            mock_cfg.hitl_internal_token = "test-token"
            mock_cfg.server_port = 8420
            mock_cfg.telegram_owner_chat_id = 999

            mock_entry = MagicMock()
            mock_entry.event = MagicMock()
            mock_entry.event.wait = AsyncMock()
            mock_entry.approved = None
            mock_store.create.return_value = ("tracktoken", mock_entry)

            # Mock the Telegram API response
            mock_tg_response = AsyncMock()
            mock_tg_response.status = 200
            mock_tg_response.json = AsyncMock(return_value={"result": {"message_id": 77}})
            mock_tg_response.__aenter__ = AsyncMock(return_value=mock_tg_response)
            mock_tg_response.__aexit__ = AsyncMock(return_value=False)

            mock_session = AsyncMock()
            mock_session.post = MagicMock(return_value=mock_tg_response)
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)

            with patch("server.aiohttp.ClientSession", return_value=mock_session), \
                 patch("server._get_telegram_token", return_value="bot123"):
                from server import app, _hitl_messages
                _hitl_messages.clear()

                client = TestClient(app, raise_server_exceptions=False)
                response = client.post(
                    "/hitl/request",
                    json={"action": "test", "summary": "track test", "wait": False},
                )

                assert response.status_code == 200
                assert "tracktoken" in _hitl_messages
                chat_id, msg_id, text = _hitl_messages["tracktoken"]
                assert chat_id == 999
                assert msg_id == 77
