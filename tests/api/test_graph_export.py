"""API tests for GET /graph/data endpoint."""

import json
from unittest.mock import patch


class TestGraphDataEndpoint:
    """Tests for GET /graph/data."""

    _SAMPLE = {"nodes": [{"id": "1", "label": "Daniel", "type": "Person", "properties": {}}], "edges": []}

    def test_returns_200(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.graph_export", return_value=json.dumps(self._SAMPLE)):
            from server import app
            from fastapi.testclient import TestClient
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/graph/data")
        assert resp.status_code == 200

    def test_response_has_nodes_and_edges(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.graph_export", return_value=json.dumps(self._SAMPLE)):
            from server import app
            from fastapi.testclient import TestClient
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/graph/data")
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data

    def test_limit_param_forwarded(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.graph_export", return_value=json.dumps(self._SAMPLE)) as mock_export:
            from server import app
            from fastapi.testclient import TestClient
            client = TestClient(app, raise_server_exceptions=False)
            client.get("/graph/data?limit=50")
        mock_export.assert_called_once_with(limit=50)

    def test_default_limit_is_400(self) -> None:
        with patch("server.session_mgr"), \
             patch("server.graph_export", return_value=json.dumps(self._SAMPLE)) as mock_export:
            from server import app
            from fastapi.testclient import TestClient
            client = TestClient(app, raise_server_exceptions=False)
            client.get("/graph/data")
        mock_export.assert_called_once_with(limit=400)
