"""API tests for broker mind CRUD endpoints: POST, PUT, DELETE /broker/minds."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def broker_client(tmp_path):
    """Create a TestClient with a real broker DB but mocked session manager."""
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
        yield client

    asyncio.get_event_loop().run_until_complete(db.close())


class TestPostBrokerMinds:
    """Tests for POST /broker/minds."""

    def test_post_broker_minds_registers_new_mind(self, broker_client):
        response = broker_client.post("/broker/minds", json={
            "name": "test",
            "gateway_url": "http://localhost:8420",
            "model": "sonnet",
            "harness": "claude_cli_claude",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["mind_id"] == "test"
        assert data["gateway_url"] == "http://localhost:8420"
        assert data["model"] == "sonnet"
        assert data["harness"] == "claude_cli_claude"

    def test_post_broker_minds_upserts_existing(self, broker_client):
        # First registration
        r1 = broker_client.post("/broker/minds", json={
            "name": "test",
            "gateway_url": "http://localhost:8420",
            "model": "sonnet",
            "harness": "claude_cli_claude",
        })
        assert r1.status_code == 200

        # Second registration with different model
        r2 = broker_client.post("/broker/minds", json={
            "name": "test",
            "gateway_url": "http://localhost:8420",
            "model": "opus",
            "harness": "claude_cli_claude",
        })
        assert r2.status_code == 200

        # Verify GET reflects updated model
        r3 = broker_client.get("/broker/minds")
        minds = r3.json()
        test_mind = [m for m in minds if m["mind_id"] == "test"]
        assert len(test_mind) == 1
        assert test_mind[0]["model"] == "opus"

    def test_post_broker_minds_missing_required_field(self, broker_client):
        response = broker_client.post("/broker/minds", json={
            "gateway_url": "http://localhost:8420",
            "model": "sonnet",
            "harness": "claude_cli_claude",
        })
        assert response.status_code == 422


class TestPutBrokerMinds:
    """Tests for PUT /broker/minds/{name}."""

    def _register(self, client, name="test"):
        client.post("/broker/minds", json={
            "name": name,
            "gateway_url": "http://localhost:8420",
            "model": "sonnet",
            "harness": "claude_cli_claude",
        })

    def test_put_broker_minds_updates_fields(self, broker_client):
        self._register(broker_client)
        response = broker_client.put("/broker/minds/test", json={
            "model": "opus",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["model"] == "opus"
        assert data["gateway_url"] == "http://localhost:8420"  # unchanged

    def test_put_broker_minds_not_found(self, broker_client):
        response = broker_client.put("/broker/minds/nonexistent", json={
            "model": "opus",
        })
        assert response.status_code == 404

    def test_put_broker_minds_empty_body_ok(self, broker_client):
        self._register(broker_client)
        response = broker_client.put("/broker/minds/test", json={})
        assert response.status_code == 200
        data = response.json()
        assert data["mind_id"] == "test"


class TestDeleteBrokerMinds:
    """Tests for DELETE /broker/minds/{name}."""

    def _register(self, client, name="test"):
        client.post("/broker/minds", json={
            "name": name,
            "gateway_url": "http://localhost:8420",
            "model": "sonnet",
            "harness": "claude_cli_claude",
        })

    def test_delete_broker_minds_removes(self, broker_client):
        self._register(broker_client)
        response = broker_client.delete("/broker/minds/test")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["name"] == "test"

        # Verify GET confirms absent
        r = broker_client.get("/broker/minds")
        ids = [m["mind_id"] for m in r.json()]
        assert "test" not in ids

    def test_delete_broker_minds_not_found(self, broker_client):
        response = broker_client.delete("/broker/minds/nonexistent")
        assert response.status_code == 404
