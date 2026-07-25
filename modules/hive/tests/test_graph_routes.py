"""API tests for /graph and /graph/data endpoints."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from starlette.testclient import TestClient

import auth
import main as main_mod

_SAMPLE = {
    "nodes": [
        {"id": "1", "label": "Daniel", "type": "Person", "properties": {}},
        {"id": "2", "label": "Python", "type": "Concept", "properties": {}},
    ],
    "edges": [
        {"source": "1", "target": "2", "label": "KNOWS"},
    ],
}

_EMPTY: dict[str, object] = {"nodes": [], "edges": []}
_ERROR: dict[str, object] = {"nodes": [], "edges": [], "error": "connection refused"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    auth.init_auth_db()
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_graph_data_returns_json(client):
    """GET /graph/data returns 200 with JSON body containing 'nodes' and 'edges'."""
    with patch("main.get_graph_data", return_value=_SAMPLE):
        resp = client.get("/graph/data")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1


def test_graph_data_forwards_limit_param(client):
    """limit query param is forwarded to get_graph_data."""
    with patch("main.get_graph_data", return_value=_EMPTY) as mock_fn:
        client.get("/graph/data?limit=50")
    mock_fn.assert_called_once_with(
        main_mod._lucent_base_url(),
        limit=50,
        bearer_token=main_mod._lucent_bearer_token(),
    )


def test_graph_data_default_limit(client):
    """Default limit of 400 is used when not specified."""
    with patch("main.get_graph_data", return_value=_EMPTY) as mock_fn:
        client.get("/graph/data")
    mock_fn.assert_called_once_with(
        main_mod._lucent_base_url(),
        limit=400,
        bearer_token=main_mod._lucent_bearer_token(),
    )


def test_graph_data_propagates_upstream_error(client):
    """When upstream returns error dict, endpoint still returns 200 with empty lists."""
    with patch("main.get_graph_data", return_value=_ERROR):
        resp = client.get("/graph/data")
    assert resp.status_code == 200
    data = resp.json()
    assert data["nodes"] == []
    assert data["edges"] == []


def test_graph_page_returns_html(client):
    """GET /graph returns 200 with HTML content type."""
    resp = client.get("/graph")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_graph_page_contains_cytoscape_reference(client):
    """GET /graph response body contains 'cytoscape' (CDN script tag)."""
    resp = client.get("/graph")
    assert "cytoscape" in resp.text.lower()


def test_graph_requires_authentication(tmp_path, monkeypatch):
    """GET /graph redirects to login when the request is unauthenticated."""
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    auth.init_auth_db()
    client = TestClient(main_mod.app)

    resp = client.get("/graph", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?next=/graph")
