"""Unit tests for graph_data module -- HTTP fetch logic."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from graph_data import get_graph_data

_SAMPLE_RESPONSE = {
    "nodes": [
        {"id": "1", "label": "Daniel", "type": "Person", "properties": {}},
        {"id": "2", "label": "Python", "type": "Concept", "properties": {}},
    ],
    "edges": [
        {"source": "1", "target": "2", "label": "KNOWS"},
    ],
}


def _mock_urlopen(response_data: dict):
    """Return a context-manager mock for urllib.request.urlopen."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=mock_resp)


def test_returns_nodes_and_edges():
    """Function returns dict with 'nodes' and 'edges' from API response."""
    with patch("urllib.request.urlopen", _mock_urlopen(_SAMPLE_RESPONSE)):
        result = get_graph_data("http://server:8420")
    assert "nodes" in result
    assert "edges" in result
    assert len(result["nodes"]) == 2
    assert len(result["edges"]) == 1


def _called_url(mock_open) -> str:
    """Pull the URL string out of the urllib Request passed to urlopen."""
    arg = mock_open.call_args[0][0]
    return arg.full_url if hasattr(arg, "full_url") else arg


def test_calls_correct_url():
    """Calls /graph/data on the provided base URL."""
    mock_open = _mock_urlopen(_SAMPLE_RESPONSE)
    with patch("urllib.request.urlopen", mock_open):
        get_graph_data("http://server:8420")
    assert _called_url(mock_open).startswith("http://server:8420/graph/data")


def test_limit_included_in_url():
    """limit param is appended to the request URL."""
    mock_open = _mock_urlopen(_SAMPLE_RESPONSE)
    with patch("urllib.request.urlopen", mock_open):
        get_graph_data("http://server:8420", limit=50)
    assert "limit=50" in _called_url(mock_open)


def test_default_limit_is_400():
    """Default limit of 400 is included in the URL."""
    mock_open = _mock_urlopen(_SAMPLE_RESPONSE)
    with patch("urllib.request.urlopen", mock_open):
        get_graph_data("http://server:8420")
    assert "limit=400" in _called_url(mock_open)


def test_trailing_slash_stripped_from_base_url():
    """Trailing slash on base URL does not produce double slash."""
    mock_open = _mock_urlopen(_SAMPLE_RESPONSE)
    with patch("urllib.request.urlopen", mock_open):
        get_graph_data("http://server:8420/")
    called_url = _called_url(mock_open)
    assert "//" not in called_url.replace("http://", "")


def test_bearer_token_sent_as_authorization_header():
    """Bearer token is attached to the request as an Authorization header."""
    mock_open = _mock_urlopen(_SAMPLE_RESPONSE)
    with patch("urllib.request.urlopen", mock_open):
        get_graph_data("http://server:8420", bearer_token="t0k3n")
    req = mock_open.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer t0k3n"


def test_no_authorization_header_without_bearer_token():
    """Without a bearer token, no Authorization header is set."""
    mock_open = _mock_urlopen(_SAMPLE_RESPONSE)
    with patch("urllib.request.urlopen", mock_open):
        get_graph_data("http://server:8420")
    req = mock_open.call_args[0][0]
    assert req.get_header("Authorization") is None


def test_network_error_returns_error_dict():
    """Connection failure returns empty lists and error key."""
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        result = get_graph_data("http://server:8420")
    assert result["nodes"] == []
    assert result["edges"] == []
    assert "error" in result


def test_empty_response_returns_empty_lists():
    """API returning empty nodes/edges is passed through correctly."""
    empty = {"nodes": [], "edges": []}
    with patch("urllib.request.urlopen", _mock_urlopen(empty)):
        result = get_graph_data("http://server:8420")
    assert result["nodes"] == []
    assert result["edges"] == []
