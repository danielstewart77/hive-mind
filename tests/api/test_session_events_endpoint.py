"""API tests for GET /sessions/{id}/events."""

import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


async def _sample_stream(session_id: str):
    assert session_id == "sess-1"
    yield {"type": "assistant", "content": "hello"}
    yield {"type": "session_closed", "session_id": session_id}


class TestSessionEventsEndpoint:
    """Passive observer endpoint tests."""

    def test_events_returns_sse_stream(self):
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.get_session = AsyncMock(
                return_value={"id": "sess-1", "status": "running"}
            )
            mock_mgr.stream_session_events = _sample_stream

            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/sessions/sess-1/events")

            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            assert (
                f"data: {json.dumps({'type': 'assistant', 'content': 'hello'})}"
                in response.text
            )

    def test_events_returns_404_for_missing_session(self):
        with patch("server.session_mgr") as mock_mgr:
            mock_mgr.get_session = AsyncMock(return_value=None)

            from server import app

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/sessions/missing/events")

            assert response.status_code == 404
            assert response.json()["error"] == "Session not found"
