"""Integration tests for digest generation via gateway."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.epilogue import TranscriptTurn, generate_digest


class TestGenerateDigest:
    """Tests for generate_digest function calling gateway."""

    @pytest.mark.asyncio
    async def test_generate_digest_calls_gateway(self):
        """Mocks the gateway client, asserts query() is called and returns structured digest."""
        mock_gateway = AsyncMock()
        mock_digest_response = json.dumps({
            "digest": "This session covered architecture decisions.",
            "topics": ["Payment service architecture", "Event sourcing design"],
            "entities": [
                {"name": "Daniel", "type": "person", "context": "project lead"},
            ],
        })
        mock_gateway.query = AsyncMock(return_value=mock_digest_response)

        turns = [
            TranscriptTurn(role="user", content="Let's discuss architecture", timestamp="t1"),
            TranscriptTurn(role="assistant", content="Sure, what aspects?", timestamp="t2"),
            TranscriptTurn(role="user", content="Payment service", timestamp="t3"),
            TranscriptTurn(role="assistant", content="I suggest microservices", timestamp="t4"),
        ]

        result = await generate_digest(mock_gateway, 123, turns)

        mock_gateway.query.assert_called_once()
        assert result is not None
        assert "digest" in result
        assert "topics" in result
        assert "entities" in result
        assert len(result["topics"]) == 2

    @pytest.mark.asyncio
    async def test_generate_digest_returns_none_on_gateway_error(self):
        """Mocks gateway to raise, asserts None returned."""
        mock_gateway = AsyncMock()
        mock_gateway.query = AsyncMock(side_effect=Exception("Gateway unreachable"))

        turns = [
            TranscriptTurn(role="user", content="Hello", timestamp="t1"),
        ]

        result = await generate_digest(mock_gateway, 123, turns)
        assert result is None
