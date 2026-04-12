"""API tests for the 503 error contract when a mind container is unreachable.

When a mind's container is down, broker message posting and session creation
should return 503 fast-fail with a structured error body.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def error_client(tmp_path):
    """Create a TestClient with broker DB for error contract testing."""
    import core.broker as broker_mod

    db_path = str(tmp_path / "broker.db")
    db = asyncio.get_event_loop().run_until_complete(broker_mod.init_db(db_path))

    mock_mgr = AsyncMock()
    mock_mgr.start = AsyncMock()
    mock_mgr.shutdown = AsyncMock()

    with patch("server.session_mgr", mock_mgr), \
         patch("core.broker.wakeup_and_collect", new_callable=AsyncMock):
        from server import app
        app.state.broker_db = db

        client = TestClient(app, raise_server_exceptions=False)
        yield client, mock_mgr

    asyncio.get_event_loop().run_until_complete(db.close())


class TestMindUnreachableError:
    """Tests for 503 fast-fail error contract."""

    def test_create_session_returns_503_when_spawn_fails_with_connection_error(self, error_client):
        """ConnectionError during session creation returns 503."""
        client, mock_mgr = error_client
        mock_mgr.create_session = AsyncMock(
            side_effect=ConnectionError("mind container unreachable")
        )

        response = client.post("/sessions", json={
            "owner_type": "test",
            "owner_ref": "user-1",
            "client_ref": "client-1",
            "mind_id": "bilby",
        })
        assert response.status_code == 503
        data = response.json()
        assert data["mind_id"] == "bilby"
        assert data["error"] == "mind_unreachable"
