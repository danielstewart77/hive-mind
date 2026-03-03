"""Tests for HITL digest approval flow in core/epilogue.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.epilogue import request_digest_approval, HITL_DIGEST_TTL


class TestRequestDigestApproval:
    """Tests for request_digest_approval function."""

    @pytest.mark.asyncio
    async def test_request_digest_approval_sends_hitl_request(self):
        """Mocks aiohttp, asserts POST to /hitl/request with correct payload."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"approved": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.epilogue.aiohttp.ClientSession", return_value=mock_session):
            result = await request_digest_approval(
                "http://localhost:8420", "This is a test digest"
            )

        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert "/hitl/request" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["action"] == "session_epilogue"
        assert payload["wait"] is True
        assert payload["ttl"] == HITL_DIGEST_TTL
        assert result is True

    @pytest.mark.asyncio
    async def test_request_digest_approval_returns_true_on_approve(self):
        """Mocks response with approved=true, asserts function returns True."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"approved": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.epilogue.aiohttp.ClientSession", return_value=mock_session):
            result = await request_digest_approval(
                "http://localhost:8420", "Test digest"
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_request_digest_approval_returns_false_on_deny(self):
        """Mocks response with approved=false, asserts function returns False."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"approved": False})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.epilogue.aiohttp.ClientSession", return_value=mock_session):
            result = await request_digest_approval(
                "http://localhost:8420", "Test digest"
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_request_digest_approval_returns_false_on_timeout(self):
        """Mocks timeout exception, asserts function returns False."""
        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=asyncio.TimeoutError())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.epilogue.aiohttp.ClientSession", return_value=mock_session):
            result = await request_digest_approval(
                "http://localhost:8420", "Test digest"
            )
        assert result is False
