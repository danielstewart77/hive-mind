"""Tests for the rule: graph_query/search_person/raw-properties surface
mind_id only on Mind nodes.

mind_id is writer-provenance, not a node attribute. Returning it on
every node confuses readers into thinking it's part of the entity. The
Mind node is the exception — there mind_id identifies the mind itself.

Run: python -m unittest lucent_api.tests.test_graph_query_mind_id
"""

from __future__ import annotations

import json
import sqlite3
import time
import unittest
from unittest.mock import patch


def _make_test_conn() -> sqlite3.Connection:
    """In-memory SQLite with the minimal nodes/edges schema graph_query needs."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE nodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mind_id    TEXT    NOT NULL,
            type        TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            first_name  TEXT,
            last_name   TEXT,
            properties  TEXT    DEFAULT '{}',
            data_class  TEXT,
            tier        TEXT,
            source      TEXT,
            as_of       TEXT,
            created_at  REAL,
            updated_at  REAL
        );
        CREATE TABLE edges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mind_id    TEXT    NOT NULL,
            source_id   INTEGER NOT NULL,
            target_id   INTEGER NOT NULL,
            type        TEXT    NOT NULL,
            as_of       TEXT,
            source      TEXT,
            data_class  TEXT,
            tier        TEXT,
            created_at  REAL,
            properties  TEXT    NOT NULL DEFAULT '{}'
        );
    """)
    now = time.time()
    conn.execute(
        "INSERT INTO nodes (mind_id, type, name, properties, data_class, tier, created_at) "
        "VALUES (?, 'Mind', 'TestMind', '{}', 'current-state', 'contextual', ?)",
        ("testmind-uuid", now),
    )
    conn.execute(
        "INSERT INTO nodes (mind_id, type, name, first_name, properties, data_class, tier, created_at) "
        "VALUES (?, 'Person', 'TestPerson', 'Test', '{}', 'current-state', 'contextual', ?)",
        ("testmind-uuid", now),
    )
    conn.commit()
    return conn


class AgentIdSurfacingTests(unittest.TestCase):
    def test_mind_node_includes_mind_id(self):
        conn = _make_test_conn()
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_query
            result = json.loads(graph_query("TestMind"))
        self.assertTrue(result["found"])
        props = result["matches"][0]["properties"]
        self.assertEqual(props["mind_id"], "testmind-uuid")

    def test_person_node_omits_mind_id(self):
        conn = _make_test_conn()
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_query
            result = json.loads(graph_query("TestPerson"))
        self.assertTrue(result["found"])
        props = result["matches"][0]["properties"]
        self.assertNotIn("mind_id", props)


if __name__ == "__main__":
    unittest.main()
