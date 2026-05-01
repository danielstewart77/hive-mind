"""Tests for tools/stateful/group_chat.py — forward_to_mind MCP tool."""

import inspect
import json
from unittest.mock import MagicMock, patch


class TestForwardToMindInterface:
    """Verify forward_to_mind function exists and has correct signature."""

    def test_forward_to_mind_function_exists(self):
        from nervous_system.inter_mind_api.group_chat import forward_to_mind
        assert callable(forward_to_mind)

    def test_forward_to_mind_has_correct_signature(self):
        from nervous_system.inter_mind_api.group_chat import forward_to_mind
        sig = inspect.signature(forward_to_mind)
        params = list(sig.parameters.keys())
        assert "mind_id" in params
        assert "message" in params
        assert "group_session_id" in params

    def test_group_chat_tools_list_exports(self):
        from nervous_system.inter_mind_api.group_chat import GROUP_CHAT_TOOLS, forward_to_mind
        assert forward_to_mind in GROUP_CHAT_TOOLS


class TestForwardToMindBehavior:
    """Verify forward_to_mind makes correct gateway calls."""

    def test_forward_to_mind_returns_json_string(self):
        from nervous_system.inter_mind_api.group_chat import forward_to_mind

        mock_sessions_resp = MagicMock()
        mock_sessions_resp.json.return_value = []
        mock_sessions_resp.raise_for_status = MagicMock()

        mock_create_resp = MagicMock()
        mock_create_resp.json.return_value = {"id": "child-sess-1"}
        mock_create_resp.raise_for_status = MagicMock()

        mock_msg_resp = MagicMock()
        mock_msg_resp.raise_for_status = MagicMock()
        mock_msg_resp.iter_lines.return_value = [
            'data: {"type": "result", "result": "Hello from Nagatha"}'
        ]

        with patch("tools.stateful.group_chat.requests") as mock_requests:
            mock_requests.get.return_value = mock_sessions_resp
            mock_requests.post.side_effect = [mock_create_resp, mock_msg_resp]

            result = forward_to_mind(
                mind_id="nagatha",
                message="Hello",
                group_session_id="group-1",
            )

        parsed = json.loads(result)
        assert "response" in parsed

    def test_forward_to_mind_creates_child_session_if_needed(self):
        from nervous_system.inter_mind_api.group_chat import forward_to_mind

        mock_sessions_resp = MagicMock()
        mock_sessions_resp.json.return_value = []  # No existing sessions
        mock_sessions_resp.raise_for_status = MagicMock()

        mock_create_resp = MagicMock()
        mock_create_resp.json.return_value = {"id": "new-child"}
        mock_create_resp.raise_for_status = MagicMock()

        mock_msg_resp = MagicMock()
        mock_msg_resp.raise_for_status = MagicMock()
        mock_msg_resp.iter_lines.return_value = [
            'data: {"type": "result", "result": "response"}'
        ]

        with patch("tools.stateful.group_chat.requests") as mock_requests:
            mock_requests.get.return_value = mock_sessions_resp
            mock_requests.post.side_effect = [mock_create_resp, mock_msg_resp]

            forward_to_mind(
                mind_id="nagatha",
                message="test",
                group_session_id="group-2",
            )

            # Second call (after get) should be create session
            create_call = mock_requests.post.call_args_list[0]
            assert "sessions" in create_call.args[0]
            assert create_call.kwargs["json"]["mind_id"] == "nagatha"
