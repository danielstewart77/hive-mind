"""API tests for message endpoint logging — entry and exit with elapsed timing."""

from unittest.mock import patch

from fastapi.testclient import TestClient


class TestMessageEndpointLogging:
    """Verify the message endpoint emits INFO logs on entry and exit."""

    def test_message_endpoint_logs_entry_on_request(self):
        """POST /sessions/{id}/message must log an INFO entry with session= and chars=."""
        with patch("server.session_mgr") as mock_mgr:

            async def mock_send_message(session_id, content, **kwargs):
                yield {"type": "result", "result": "ok", "session_id": None}

            mock_mgr.send_message = mock_send_message

            from server import app

            with patch("server.log") as mock_log:
                client = TestClient(app, raise_server_exceptions=False)
                response = client.post(
                    "/sessions/test-id/message",
                    json={"content": "Hello world"},
                )

                assert response.status_code == 200

                # Find an info call that contains "message:" and "session=test-id" and "chars="
                info_calls = [
                    call for call in mock_log.info.call_args_list
                    if len(call.args) > 0 and "message:" in str(call.args[0])
                    and "session=" in str(call.args)
                    and "chars=" in str(call.args[0])
                ]
                assert len(info_calls) >= 1, (
                    f"Expected at least 1 entry log with 'message:' and 'session=' and 'chars=', "
                    f"got: {mock_log.info.call_args_list}"
                )

    def test_message_endpoint_logs_exit_with_elapsed(self):
        """POST /sessions/{id}/message must log INFO with 'message: done' and 'elapsed='."""
        with patch("server.session_mgr") as mock_mgr:

            async def mock_send_message(session_id, content, **kwargs):
                yield {"type": "result", "result": "ok", "session_id": None}

            mock_mgr.send_message = mock_send_message

            from server import app

            with patch("server.log") as mock_log:
                client = TestClient(app, raise_server_exceptions=False)
                response = client.post(
                    "/sessions/test-id/message",
                    json={"content": "Hello world"},
                )

                assert response.status_code == 200

                # Find an info call with "message: done" and "elapsed="
                done_calls = [
                    call for call in mock_log.info.call_args_list
                    if len(call.args) > 0
                    and "message: done" in str(call.args[0])
                    and "elapsed=" in str(call.args[0])
                ]
                assert len(done_calls) >= 1, (
                    f"Expected at least 1 exit log with 'message: done' and 'elapsed=', "
                    f"got: {mock_log.info.call_args_list}"
                )
