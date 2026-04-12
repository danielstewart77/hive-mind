"""API tests for POST/DELETE /secrets/scopes and GET /secrets/scopes/{mind_name}.

These endpoints manage secret scoping policy and require HITL internal token.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def scope_client(tmp_path):
    """Create a TestClient with real broker DB for scope testing."""
    import core.broker as broker_mod

    db_path = str(tmp_path / "broker.db")
    db = asyncio.get_event_loop().run_until_complete(broker_mod.init_db(db_path))

    mock_mgr = AsyncMock()
    mock_mgr.start = AsyncMock()
    mock_mgr.shutdown = AsyncMock()

    with patch("server.session_mgr", mock_mgr), \
         patch("server.config") as mock_config:
        mock_config.hitl_internal_token = "test-secret-token"
        mock_config.server_port = 8420

        from server import app
        app.state.broker_db = db

        client = TestClient(app, raise_server_exceptions=False)
        yield client

    asyncio.get_event_loop().run_until_complete(db.close())


class TestGrantSecretScope:
    """Tests for POST /secrets/scopes."""

    def test_grant_secret_scope_returns_200(self, scope_client):
        response = scope_client.post(
            "/secrets/scopes",
            json={"mind_name": "ada", "secret_keys": ["KEY_A", "KEY_B"]},
            headers={"x-hitl-internal": "test-secret-token"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["mind_name"] == "ada"
        assert data["granted"] == ["KEY_A", "KEY_B"]

    def test_grant_secret_scope_unauthorized_without_token(self, scope_client):
        response = scope_client.post(
            "/secrets/scopes",
            json={"mind_name": "ada", "secret_keys": ["KEY_A"]},
        )
        assert response.status_code == 401


class TestRevokeSecretScope:
    """Tests for DELETE /secrets/scopes."""

    def test_revoke_secret_scope_returns_200(self, scope_client):
        # First grant
        scope_client.post(
            "/secrets/scopes",
            json={"mind_name": "ada", "secret_keys": ["KEY_A"]},
            headers={"x-hitl-internal": "test-secret-token"},
        )
        # Then revoke
        response = scope_client.request(
            "DELETE",
            "/secrets/scopes",
            json={"mind_name": "ada", "secret_keys": ["KEY_A"]},
            headers={"x-hitl-internal": "test-secret-token"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True


class TestGetSecretScopes:
    """Tests for GET /secrets/scopes/{mind_name}."""

    def test_get_secret_scopes_returns_list(self, scope_client):
        # Grant some scopes first
        scope_client.post(
            "/secrets/scopes",
            json={"mind_name": "ada", "secret_keys": ["KEY_A", "KEY_B"]},
            headers={"x-hitl-internal": "test-secret-token"},
        )

        response = scope_client.get(
            "/secrets/scopes/ada",
            headers={"x-hitl-internal": "test-secret-token"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["mind_name"] == "ada"
        assert sorted(data["secret_keys"]) == ["KEY_A", "KEY_B"]
