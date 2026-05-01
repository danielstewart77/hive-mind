"""Unit tests for structured logging in tools/stateful/group_chat.py — forward_to_mind lifecycle."""

import json
from unittest.mock import MagicMock, patch


def _build_mock_requests(existing_sessions=None, child_session_id="child-1", response_text="ok"):
    """Build mock requests module for forward_to_mind tests."""
    mock_sessions_resp = MagicMock()
    mock_sessions_resp.json.return_value = existing_sessions or []
    mock_sessions_resp.raise_for_status = MagicMock()

    mock_create_resp = MagicMock()
    mock_create_resp.json.return_value = {"id": child_session_id}
    mock_create_resp.raise_for_status = MagicMock()

    mock_msg_resp = MagicMock()
    mock_msg_resp.raise_for_status = MagicMock()
    mock_msg_resp.iter_lines.return_value = [
        f'data: {{"type": "result", "result": "{response_text}"}}'
    ]

    return mock_sessions_resp, mock_create_resp, mock_msg_resp


class TestForwardToMindLogging:
    """Verify forward_to_mind emits structured logs at lifecycle boundaries."""

    def test_forward_to_mind_logs_start(self):
        """forward_to_mind must log INFO 'forward_to_mind: start' with mind= and group=."""
        mock_sessions_resp, mock_create_resp, mock_msg_resp = _build_mock_requests()

        with (
            patch("tools.stateful.group_chat.requests") as mock_requests,
            patch("tools.stateful.group_chat.logger") as mock_logger,
        ):
            mock_requests.get.return_value = mock_sessions_resp
            mock_requests.post.side_effect = [mock_create_resp, mock_msg_resp]

            from nervous_system.inter_mind_api.group_chat import forward_to_mind
            forward_to_mind(
                mind_id="nagatha",
                message="Hello",
                group_session_id="group-1",
            )

            start_calls = [
                c for c in mock_logger.info.call_args_list
                if len(c.args) > 0 and "forward_to_mind: start" in str(c.args[0])
            ]
            assert len(start_calls) >= 1, (
                f"Expected 'forward_to_mind: start' log, got: {mock_logger.info.call_args_list}"
            )
            # Verify mind= and group= present in args
            call_str = str(start_calls[0])
            assert "nagatha" in call_str
            assert "group-1" in call_str

    def test_forward_to_mind_logs_session_found(self):
        """forward_to_mind must log INFO 'forward_to_mind: using session=' when a session is found."""
        existing = [{
            "id": "existing-child",
            "mind_id": "nagatha",
            "owner_ref": "group-2",
            "status": "running",
        }]
        mock_sessions_resp, _, mock_msg_resp = _build_mock_requests(
            existing_sessions=existing, child_session_id="existing-child"
        )

        with (
            patch("tools.stateful.group_chat.requests") as mock_requests,
            patch("tools.stateful.group_chat.logger") as mock_logger,
        ):
            mock_requests.get.return_value = mock_sessions_resp
            # Only message post, no create needed since session exists
            mock_requests.post.return_value = mock_msg_resp

            from nervous_system.inter_mind_api.group_chat import forward_to_mind
            forward_to_mind(
                mind_id="nagatha",
                message="Hello",
                group_session_id="group-2",
            )

            session_calls = [
                c for c in mock_logger.info.call_args_list
                if len(c.args) > 0 and "forward_to_mind: using session=" in str(c.args[0])
            ]
            assert len(session_calls) >= 1, (
                f"Expected 'forward_to_mind: using session=' log, got: {mock_logger.info.call_args_list}"
            )

    def test_forward_to_mind_logs_completion_with_elapsed(self):
        """forward_to_mind must log INFO 'forward_to_mind: done' with mind= and elapsed=."""
        mock_sessions_resp, mock_create_resp, mock_msg_resp = _build_mock_requests()

        with (
            patch("tools.stateful.group_chat.requests") as mock_requests,
            patch("tools.stateful.group_chat.logger") as mock_logger,
        ):
            mock_requests.get.return_value = mock_sessions_resp
            mock_requests.post.side_effect = [mock_create_resp, mock_msg_resp]

            from nervous_system.inter_mind_api.group_chat import forward_to_mind
            forward_to_mind(
                mind_id="nagatha",
                message="Hello",
                group_session_id="group-3",
            )

            done_calls = [
                c for c in mock_logger.info.call_args_list
                if len(c.args) > 0 and "forward_to_mind: done" in str(c.args[0])
                and "elapsed=" in str(c.args[0])
            ]
            assert len(done_calls) >= 1, (
                f"Expected 'forward_to_mind: done' with 'elapsed=', got: {mock_logger.info.call_args_list}"
            )

    def test_forward_to_mind_logs_timeout_at_error(self, monkeypatch):
        """forward_to_mind must log ERROR 'forward_to_mind: timeout' on ReadTimeout."""
        import importlib
        import sys

        # Temporarily restore real requests module so we can get the exception classes
        saved = sys.modules.get("requests")
        if saved and not hasattr(saved, "__file__"):
            # It's a mock — remove it so we can import the real one
            del sys.modules["requests"]
            # Also remove submodules
            for key in list(sys.modules):
                if key.startswith("requests."):
                    del sys.modules[key]

        real_requests = importlib.import_module("requests")
        ReadTimeout = real_requests.exceptions.ReadTimeout
        real_exceptions = real_requests.exceptions

        # Restore the mock if there was one
        if saved is not None:
            sys.modules["requests"] = saved

        mock_sessions_resp, mock_create_resp, _ = _build_mock_requests()

        with (
            patch("tools.stateful.group_chat.requests") as mock_requests,
            patch("tools.stateful.group_chat.logger") as mock_logger,
        ):
            # Wire real exception classes so the except clause can match
            mock_requests.exceptions = real_exceptions
            mock_requests.get.return_value = mock_sessions_resp
            # First post creates session, second raises ReadTimeout
            mock_requests.post.side_effect = [
                mock_create_resp,
                ReadTimeout("Timed out"),
            ]

            from nervous_system.inter_mind_api.group_chat import forward_to_mind
            result = forward_to_mind(
                mind_id="bob",
                message="Hello",
                group_session_id="group-4",
            )

            # Should return error JSON
            parsed = json.loads(result)
            assert "error" in parsed

            error_calls = [
                c for c in mock_logger.error.call_args_list
                if len(c.args) > 0 and "forward_to_mind: timeout" in str(c.args[0])
            ]
            assert len(error_calls) >= 1, (
                f"Expected 'forward_to_mind: timeout' error log, got: {mock_logger.error.call_args_list}"
            )
