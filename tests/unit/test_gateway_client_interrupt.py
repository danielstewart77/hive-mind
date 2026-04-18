"""Unit tests for GatewayClient.interrupt_session().

Covers: correct endpoint call and handling of error responses.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.gateway_client import GatewayClient


@pytest.fixture()
def gateway():
    """Create a GatewayClient with a mocked HTTP session."""
    mock_http = MagicMock()
    return GatewayClient(
        http=mock_http,
        server_url="http://localhost:8420",
        owner_type="telegram:ada",
        mind_id="ada",
    )


class TestGatewayClientInterrupt:
    """GatewayClient.interrupt_session() tests."""

    @pytest.mark.asyncio
    async def test_interrupt_session_calls_correct_endpoint(self, gateway):
        """interrupt_session() POSTs to /sessions/{id}/interrupt and returns the JSON response."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"ok": True, "session_id": "sess-1"})

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        gateway.http.post = MagicMock(return_value=mock_ctx)

        result = await gateway.interrupt_session("sess-1")

        assert result == {"ok": True, "session_id": "sess-1"}
        gateway.http.post.assert_called_once_with(
            "http://localhost:8420/sessions/sess-1/interrupt"
        )

    @pytest.mark.asyncio
    async def test_interrupt_session_returns_response_on_404(self, gateway):
        """interrupt_session() returns the error response dict when gateway returns 404."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(
            return_value={"error": "Session not found: nonexistent"}
        )

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        gateway.http.post = MagicMock(return_value=mock_ctx)

        result = await gateway.interrupt_session("nonexistent")

        assert "error" in result


class TestGatewayClientFindActiveSession:
    """GatewayClient.find_active_session() tests."""

    @pytest.mark.asyncio
    async def test_find_active_session_returns_id_when_active_exists(self, gateway):
        """find_active_session() returns session ID when an active session exists."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value=[
            {"id": "sess-1", "is_active": True},
        ])

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        gateway.http.get = MagicMock(return_value=mock_ctx)

        result = await gateway.find_active_session(123, 456)

        assert result == "sess-1"
        gateway.http.get.assert_called_once_with(
            "http://localhost:8420/sessions",
            params={"client_type": "telegram:ada", "client_ref": "456"},
        )

    @pytest.mark.asyncio
    async def test_find_active_session_returns_none_when_no_active(self, gateway):
        """find_active_session() returns None when no active session exists."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value=[
            {"id": "sess-old", "is_active": False},
        ])

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        gateway.http.get = MagicMock(return_value=mock_ctx)

        result = await gateway.find_active_session(123, 456)

        assert result is None

    @pytest.mark.asyncio
    async def test_find_active_session_returns_none_when_no_sessions(self, gateway):
        """find_active_session() returns None when no sessions exist at all."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value=[])

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        gateway.http.get = MagicMock(return_value=mock_ctx)

        result = await gateway.find_active_session(123, 456)

        assert result is None
