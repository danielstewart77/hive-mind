"""API tests for GET /graph/data endpoint.

Post-Phase-1 consolidation refactor: the endpoint now reads sqlite directly
inside the handler rather than delegating to a `graph_export()` helper. Tests
patch the connection layer instead.
"""

import sqlite3
from unittest.mock import MagicMock, patch


class TestGraphDataEndpoint:
    """Tests for GET /graph/data."""

    def _mock_connect(self, nodes=None, edges=None):
        nodes = nodes or [(1, "Daniel", "Person", "{}")]
        edges = edges or []

        def _connect(*args, **kwargs):
            conn = MagicMock()
            conn.row_factory = None

            def _execute(sql, params=()):
                cur = MagicMock()
                if sql.strip().lower().startswith("select id, name, type, properties from nodes"):
                    cur.__iter__ = lambda self: iter([
                        sqlite3.Row.__call__ if False else _row(("id", "name", "type", "properties"), n)
                        for n in nodes
                    ])
                else:
                    cur.__iter__ = lambda self: iter([
                        _row(("source", "target", "type"), e) for e in edges
                    ])
                return cur

            conn.execute = _execute
            conn.close = MagicMock()
            return conn
        return _connect


def _row(cols, values):
    """Tiny dict-like object that supports both attribute and key access."""
    class _R:
        def __init__(self):
            for c, v in zip(cols, values):
                setattr(self, c, v)
        def __getitem__(self, k):
            return getattr(self, k)
        def keys(self):
            return cols
    return _R()


def test_returns_200_and_shape():
    """Endpoint returns 200 with `nodes` and `edges` keys."""
    from server import app
    from fastapi.testclient import TestClient

    fake_cursor = MagicMock()
    fake_cursor.fetchall.return_value = []

    fake_conn = MagicMock()
    fake_conn.row_factory = None
    fake_conn.execute.return_value = fake_cursor
    fake_conn.close = MagicMock()

    with patch("server.session_mgr"), patch("sqlite3.connect", return_value=fake_conn):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/graph/data")

    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
