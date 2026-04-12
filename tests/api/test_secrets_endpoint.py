"""API tests for GET /secrets/{key} endpoint.

The secrets endpoint identifies callers by Docker network identity (source IP),
checks secret scoping policy, and returns the secret value.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


class TestSecretsEndpoint:
    """Tests for GET /secrets/{key}."""

    def test_secrets_endpoint_returns_value_when_scoped(self):
        """Scoped caller gets the secret value."""
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.resolve_container_name", new_callable=AsyncMock, return_value="bilby"), \
             patch("server.check_secret_scope", new_callable=AsyncMock, return_value=True), \
             patch("server.get_credential", return_value="secret-value"):
            mock_mgr.start = AsyncMock()
            mock_mgr.shutdown = AsyncMock()

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/secrets/MY_KEY")

            assert response.status_code == 200
            data = response.json()
            assert data["key"] == "MY_KEY"
            assert data["value"] == "secret-value"

    def test_secrets_endpoint_returns_403_when_not_scoped(self):
        """Caller without scope gets 403."""
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.resolve_container_name", new_callable=AsyncMock, return_value="bilby"), \
             patch("server.check_secret_scope", new_callable=AsyncMock, return_value=False):
            mock_mgr.start = AsyncMock()
            mock_mgr.shutdown = AsyncMock()

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/secrets/MY_KEY")

            assert response.status_code == 403
            data = response.json()
            assert data["error"] == "forbidden"

    def test_secrets_endpoint_returns_403_when_identity_unknown(self):
        """Unresolvable caller IP gets 403."""
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.resolve_container_name", new_callable=AsyncMock, return_value=None):
            mock_mgr.start = AsyncMock()
            mock_mgr.shutdown = AsyncMock()

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/secrets/MY_KEY")

            assert response.status_code == 403

    def test_secrets_endpoint_returns_404_when_key_not_found(self):
        """Scoped caller requesting a non-existent key gets 404."""
        with patch("server.session_mgr") as mock_mgr, \
             patch("server.resolve_container_name", new_callable=AsyncMock, return_value="bilby"), \
             patch("server.check_secret_scope", new_callable=AsyncMock, return_value=True), \
             patch("server.get_credential", return_value=None):
            mock_mgr.start = AsyncMock()
            mock_mgr.shutdown = AsyncMock()

            from server import app
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/secrets/NONEXISTENT_KEY")

            assert response.status_code == 404
            data = response.json()
            assert data["error"] == "secret not found"
