"""API tests for broker endpoints in server.py."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def broker_client(tmp_path):
    """Create a TestClient with a real broker DB but mocked wakeup."""
    import core.broker as broker_mod

    db_path = str(tmp_path / "broker.db")
    db = asyncio.get_event_loop().run_until_complete(broker_mod.init_db(db_path))

    mock_mgr = AsyncMock()
    mock_mgr.start = AsyncMock()
    mock_mgr.shutdown = AsyncMock()

    with patch("server.session_mgr", mock_mgr), \
         patch("core.broker.wakeup_and_collect", new_callable=AsyncMock) as mock_wakeup:

        from server import app
        app.state.broker_db = db

        # Patch lifespan to skip broker init (we did it above)
        # We achieve this by pre-setting app.state.broker_db
        client = TestClient(app, raise_server_exceptions=False)
        yield client

    asyncio.get_event_loop().run_until_complete(db.close())


class TestGetBrokerMinds:
    """Tests for GET /broker/minds endpoint."""

    def test_get_broker_minds_returns_list(self, broker_client):
        response = broker_client.get("/broker/minds")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_get_broker_minds_contains_registered_minds(self, broker_client):
        """After startup (which scans minds/), the endpoint returns minds that have runtime.yaml files."""
        import core.broker as broker_mod

        # Manually register a mind in the DB so we can verify the endpoint returns it
        from core.broker import register_mind
        db = broker_client.app.state.broker_db

        asyncio.get_event_loop().run_until_complete(
            register_mind(
                db,
                mind_id="test_mind",
                gateway_url="http://hive_mind:8420",
                model="sonnet",
                harness="claude_cli_claude",
            )
        )

        response = broker_client.get("/broker/minds")
        assert response.status_code == 200
        data = response.json()
        ids = [m["mind_id"] for m in data]
        assert "test_mind" in ids


class TestPostBrokerMessage:
    def test_returns_dispatched(self, broker_client):
        conv_id = str(uuid.uuid4())
        response = broker_client.post("/broker/messages", json={
            "conversation_id": conv_id,
            "from_mind": "ada",
            "to_mind": "nagatha",
            "content": "Analyse the logs",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "dispatched"
        assert data["conversation_id"] == conv_id
        assert "message_id" in data

    def test_missing_required_fields(self, broker_client):
        response = broker_client.post("/broker/messages", json={
            "from_mind": "ada",
            "content": "oops no conversation_id or to_mind",
        })
        assert response.status_code == 422

    def test_unknown_mind_returns_404(self, broker_client):
        response = broker_client.post("/broker/messages", json={
            "conversation_id": str(uuid.uuid4()),
            "from_mind": "ada",
            "to_mind": "nonexistent_mind_xyz",
            "content": "hello",
        })
        assert response.status_code == 404
        assert "not found" in response.json()["error"].lower()

    def test_idempotent_on_duplicate_id(self, broker_client):
        msg_id = str(uuid.uuid4())
        conv_id = str(uuid.uuid4())
        payload = {
            "message_id": msg_id,
            "conversation_id": conv_id,
            "from_mind": "ada",
            "to_mind": "nagatha",
            "content": "hello",
        }
        r1 = broker_client.post("/broker/messages", json=payload)
        assert r1.status_code == 200
        assert r1.json()["status"] == "dispatched"

        r2 = broker_client.post("/broker/messages", json=payload)
        assert r2.status_code == 200
        assert r2.json()["status"] == "exists"

    def test_accepts_from_alias(self, broker_client):
        """POST with 'from'/'to' aliases (JSON spec) should work."""
        response = broker_client.post("/broker/messages", json={
            "conversation_id": str(uuid.uuid4()),
            "from": "ada",
            "to": "nagatha",
            "content": "test alias",
        })
        assert response.status_code == 200
        assert response.json()["status"] == "dispatched"


class TestGetBrokerMessages:
    def test_returns_messages_for_conversation(self, broker_client):
        conv_id = str(uuid.uuid4())
        broker_client.post("/broker/messages", json={
            "conversation_id": conv_id,
            "from_mind": "ada",
            "to_mind": "nagatha",
            "content": "hello",
        })

        response = broker_client.get(f"/broker/messages?conversation_id={conv_id}")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["conversation_id"] == conv_id

    def test_empty_conversation_returns_empty_list(self, broker_client):
        response = broker_client.get(f"/broker/messages?conversation_id={uuid.uuid4()}")
        assert response.status_code == 200
        assert response.json() == []


class TestGetBrokerConversation:
    def test_returns_detail(self, broker_client):
        conv_id = str(uuid.uuid4())
        broker_client.post("/broker/messages", json={
            "conversation_id": conv_id,
            "from_mind": "ada",
            "to_mind": "nagatha",
            "content": "hello",
        })

        response = broker_client.get(f"/broker/conversations/{conv_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["conversation_id"] == conv_id
        assert "messages" in data

    def test_not_found_returns_404(self, broker_client):
        response = broker_client.get(f"/broker/conversations/{uuid.uuid4()}")
        assert response.status_code == 404
