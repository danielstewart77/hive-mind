"""Tests for nervous_system.lucent_api.lucent_graph.graph_query and graph_search.

graph_query: identity-only matching (name, first_name, last_name, aliases-exact).
graph_search: full-text mention scan across all property strings.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch


def _make_test_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with Lucent schema."""
    from nervous_system.lucent_api import lucent as lucent_mod

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    lucent_mod._init_schema(conn)
    return conn


def _patch_conn(conn):
    """Patch _get_connection to return the test connection."""
    return patch(
        "nervous_system.lucent_api.lucent._get_connection",
        return_value=conn,
    )


def _insert_node(
    conn: sqlite3.Connection,
    *,
    name: str,
    type: str = "Person",
    agent_id: str = "ada",
    first_name: str | None = None,
    last_name: str | None = None,
    properties: dict | None = None,
) -> int:
    """Insert a test node directly into the SQLite DB. Returns the autoincrement id."""
    props_json = json.dumps(properties or {})
    cur = conn.execute(
        """
        INSERT INTO nodes
            (name, type, agent_id, first_name, last_name, properties,
             data_class, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'person', 'user', 0, 0)
        """,
        (name, type, agent_id, first_name, last_name, props_json),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid




def _matched_names(result: dict) -> list[str]:
    """Pull node names out of a graph_query result."""
    if not isinstance(result, dict):
        return []
    if not result.get("found"):
        return []
    return [m["properties"].get("name") for m in result.get("matches", [])]


# ---------------------------------------------------------------------------
# graph_query — identity-only matching
# ---------------------------------------------------------------------------


class TestGraphQueryIdentity:
    def test_matches_name_exact_case_insensitive(self):
        conn = _make_test_conn()
        _insert_node(conn, name="Daniel")

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result_lower = json.loads(lg.graph_query("daniel", agent_id="ada"))
            result_upper = json.loads(lg.graph_query("DANIEL", agent_id="ada"))

        assert "Daniel" in _matched_names(result_lower)
        assert "Daniel" in _matched_names(result_upper)

    def test_matches_first_name(self):
        conn = _make_test_conn()
        _insert_node(conn, name="Daniel Stewart", first_name="Daniel")

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_query("Daniel", agent_id="ada"))

        assert "Daniel Stewart" in _matched_names(result)

    def test_matches_last_name(self):
        conn = _make_test_conn()
        _insert_node(conn, name="Daniel Stewart", last_name="Stewart")

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_query("Stewart", agent_id="ada"))

        assert "Daniel Stewart" in _matched_names(result)

    def test_matches_alias_exact_in_json_list(self):
        conn = _make_test_conn()
        _insert_node(
            conn,
            name="Daniel Stewart",
            properties={"aliases": ["Dan", "Danny"]},
        )

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_query("Dan", agent_id="ada"))

        assert "Daniel Stewart" in _matched_names(result)

    def test_does_not_match_property_text_mention(self):
        """Querying for 'Daniel' must NOT return Coach Manny just because
        Manny's notes mention Daniel.
        """
        conn = _make_test_conn()
        _insert_node(
            conn,
            name="Coach Manny",
            properties={"notes": "Met Daniel at the conference"},
        )

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_query("Daniel", agent_id="ada"))

        # Either {"found": False, ...} or a dict that doesn't have Coach Manny.
        assert "Coach Manny" not in _matched_names(result)

    def test_partial_name_does_not_match_unless_alias(self):
        """`Dan` must not match a node named `Daniel` unless `Dan` is an alias."""
        conn = _make_test_conn()
        _insert_node(conn, name="Daniel")

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_query("Dan", agent_id="ada"))

        assert "Daniel" not in _matched_names(result)

    def test_empty_query_returns_no_results(self):
        conn = _make_test_conn()
        _insert_node(conn, name="Daniel")

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_query("", agent_id="ada"))

        assert isinstance(result, dict)
        assert result.get("found") is False

    def test_distinct_by_node_id_when_multiple_fields_hit(self):
        """A node matched by multiple identity fields appears once."""
        conn = _make_test_conn()
        _insert_node(
            conn,
            name="Dan",
            first_name="Dan",
            properties={"aliases": ["Dan"]},
        )

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_query("Dan", agent_id="ada"))

        names = _matched_names(result)
        assert names.count("Dan") == 1
        assert "Dan" in _matched_names(result)

    def test_alias_substring_does_not_match_partial(self):
        """An alias of 'Daniel' (only) does NOT get matched by query 'Dan'.

        Per design: alias matching is exact-element, not substring-of-element.
        """
        conn = _make_test_conn()
        _insert_node(
            conn,
            name="Daniel Stewart",
            properties={"aliases": ["Daniel"]},  # 'Dan' is NOT registered
        )

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_query("Dan", agent_id="ada"))

        assert "Daniel Stewart" not in _matched_names(result)


# ---------------------------------------------------------------------------
# graph_search — mention search
# ---------------------------------------------------------------------------


class TestGraphSearch:
    def test_returns_mention_shape(self):
        conn = _make_test_conn()
        nid = _insert_node(
            conn,
            name="Coach Manny",
            properties={"notes": "Met Daniel at the conference"},
        )

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_search("Daniel"))

        assert isinstance(result, list)
        assert len(result) == 1
        hit = result[0]
        assert hit["node_id"] == nid
        assert hit["node_type"] == "Person"
        assert hit["property"] == "notes"
        assert "Daniel" in hit["snippet"]

    def test_finds_mention_in_any_property(self):
        conn = _make_test_conn()
        nid = _insert_node(
            conn,
            name="auth-service",
            type="System",
            properties={"description": "owned by Daniel"},
        )

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_search("Daniel"))

        assert any(h["node_id"] == nid for h in result)

    def test_does_not_double_count_node(self):
        """Multiple property hits within the same node return one result."""
        conn = _make_test_conn()
        nid = _insert_node(
            conn,
            name="Coach Manny",
            properties={
                "notes": "Met Daniel at the conference",
                "bio": "Daniel is his nephew",
            },
        )

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_search("Daniel"))

        nid_hits = [h for h in result if h["node_id"] == nid]
        assert len(nid_hits) == 1

    def test_honours_limit(self):
        conn = _make_test_conn()
        for i in range(10):
            _insert_node(
                conn,
                name=f"Node{i}",
                properties={"notes": f"refs Daniel #{i}"},
            )

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_search("Daniel", limit=3))

        assert len(result) == 3

    def test_empty_query_returns_empty_list(self):
        conn = _make_test_conn()
        _insert_node(
            conn,
            name="Coach Manny",
            properties={"notes": "Met Daniel at the conference"},
        )

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_search(""))

        assert result == []

    def test_identity_node_appears_in_search_only_if_property_text_matches(self):
        """A node whose only match is its identity fields (name) does NOT
        appear in search — search scans property *values*, not identity.
        """
        conn = _make_test_conn()
        nid = _insert_node(conn, name="Daniel")  # no notes mentioning self

        from nervous_system.lucent_api import lucent_graph as lg

        with _patch_conn(conn):
            result = json.loads(lg.graph_search("Daniel"))

        assert all(h["node_id"] != nid for h in result)
