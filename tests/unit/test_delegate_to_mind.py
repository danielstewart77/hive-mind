"""Tests for tools/stateful/inter_mind.py — delegate_to_mind MCP tool."""

import inspect
import json
from unittest.mock import MagicMock, patch


class TestDelegateToMindInterface:
    """Verify delegate_to_mind function exists and has correct signature."""

    def test_delegate_to_mind_function_exists(self):
        from tools.stateful.inter_mind import delegate_to_mind
        assert callable(delegate_to_mind)

    def test_delegate_to_mind_has_correct_signature(self):
        from tools.stateful.inter_mind import delegate_to_mind
        sig = inspect.signature(delegate_to_mind)
        params = list(sig.parameters.keys())
        assert "mind_id" in params
        assert "message" in params
        assert "mode" in params
        assert "chain" in params

    def test_inter_mind_tools_list_exports(self):
        from tools.stateful.inter_mind import INTER_MIND_TOOLS, delegate_to_mind
        assert delegate_to_mind in INTER_MIND_TOOLS


class TestDelegateToMindCyclePrevention:
    """Verify cycle prevention and hop limit."""

    def test_delegate_to_mind_rejects_cycle(self):
        from tools.stateful.inter_mind import delegate_to_mind

        result = delegate_to_mind(
            mind_id="nagatha",
            message="test",
            chain=["nagatha"],
        )
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Cycle detected" in parsed["error"]

    def test_delegate_to_mind_enforces_one_hop_limit(self):
        from tools.stateful.inter_mind import delegate_to_mind

        result = delegate_to_mind(
            mind_id="nagatha",
            message="test",
            chain=["ada"],  # len >= 1
        )
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Hop limit exceeded" in parsed["error"]

    def test_delegate_to_mind_initialises_empty_chain(self):
        from tools.stateful.inter_mind import delegate_to_mind

        mock_create_resp = MagicMock()
        mock_create_resp.json.return_value = {"id": "sess-1"}
        mock_create_resp.raise_for_status = MagicMock()

        mock_msg_resp = MagicMock()
        mock_msg_resp.raise_for_status = MagicMock()
        mock_msg_resp.iter_lines.return_value = [
            'data: {"type": "result", "result": "ok"}'
        ]

        with patch("tools.stateful.inter_mind.requests") as mock_requests:
            mock_requests.post.side_effect = [mock_create_resp, mock_msg_resp]

            # chain=None should not raise cycle error
            result = delegate_to_mind(
                mind_id="nagatha",
                message="test",
                chain=None,
            )
            parsed = json.loads(result)
            assert "error" not in parsed


class TestDelegateToMindResponse:
    """Verify delegate_to_mind returns correct response."""

    def test_delegate_to_mind_returns_response_json(self):
        from tools.stateful.inter_mind import delegate_to_mind

        mock_create_resp = MagicMock()
        mock_create_resp.json.return_value = {"id": "sess-2"}
        mock_create_resp.raise_for_status = MagicMock()

        mock_msg_resp = MagicMock()
        mock_msg_resp.raise_for_status = MagicMock()
        mock_msg_resp.iter_lines.return_value = [
            'data: {"type": "result", "result": "Hello from delegate"}'
        ]

        with patch("tools.stateful.inter_mind.requests") as mock_requests:
            mock_requests.post.side_effect = [mock_create_resp, mock_msg_resp]

            result = delegate_to_mind(
                mind_id="nagatha",
                message="test",
            )
            parsed = json.loads(result)
            assert "response" in parsed
            assert parsed["inter_mind"] is True
