"""Tests for the property-safe v2 graph endpoints.

The legacy /graph/upsert path is full-replace on the properties blob and
also rewrites the source node when creating an edge. These tests cover
the v2 surface that separates the four real intents and the key
invariant that fixes the original bug: creating an edge MUST NOT touch
the source node's properties.

Run: python -m unittest lucent_api.tests.test_graph_v2_endpoints
"""

from __future__ import annotations

import json
import sqlite3
import time
import unittest
from unittest.mock import patch


def _make_test_conn() -> sqlite3.Connection:
    """In-memory SQLite with the minimal nodes/edges schema."""
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
            properties  TEXT    NOT NULL DEFAULT '{}',
            UNIQUE(source_id, target_id, type)
        );
    """)
    return conn


def _seed_person(conn, name, first_name=None, last_name=None, props=None, mind_id="testmind"):
    now = time.time()
    conn.execute(
        "INSERT INTO nodes (mind_id, type, name, first_name, last_name, properties, "
        "data_class, tier, source, as_of, created_at, updated_at) "
        "VALUES (?, 'Person', ?, ?, ?, ?, 'current-state', 'contextual', 'user', "
        "'2026-05-12T00:00:00Z', ?, ?)",
        (mind_id, name, first_name, last_name, json.dumps(props or {}), now, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM nodes WHERE name=? AND type='Person'", (name,)).fetchone()["id"]


class NodeCreateTests(unittest.TestCase):
    def test_creates_new_node(self):
        conn = _make_test_conn()
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_node_create
            result = json.loads(graph_node_create(
                entity_type="Person", name="NewPerson",
                data_class="current-state", mind_id="testmind",
                properties='{"title": "Engineer"}',
            ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "NewPerson")
        # Verify it landed
        row = conn.execute("SELECT properties FROM nodes WHERE name='NewPerson'").fetchone()
        self.assertEqual(json.loads(row["properties"])["title"], "Engineer")

    def test_rejects_existing_node(self):
        conn = _make_test_conn()
        _seed_person(conn, "Existing")
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_node_create
            result = json.loads(graph_node_create(
                entity_type="Person", name="Existing",
                data_class="current-state", mind_id="testmind",
            ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "exists")


class PropertiesMergeTests(unittest.TestCase):
    def test_merge_preserves_existing_keys(self):
        """The bug class this endpoint fixes: adding one property must
        not erase others."""
        conn = _make_test_conn()
        _seed_person(conn, "Manny", first_name="Manny",
                     props={"aliases": ["Manny", "Coach Manny"], "title": "Coach"})
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_properties_merge
            result = json.loads(graph_properties_merge(
                entity_type="Person", name="Manny",
                properties='{"birthday": "1970-01-01"}',
            ))
        self.assertTrue(result["ok"])
        row = conn.execute("SELECT properties FROM nodes WHERE name='Manny'").fetchone()
        props = json.loads(row["properties"])
        # birthday added, aliases + title still present
        self.assertEqual(props["birthday"], "1970-01-01")
        self.assertEqual(props["aliases"], ["Manny", "Coach Manny"])
        self.assertEqual(props["title"], "Coach")

    def test_merge_overwrites_named_keys(self):
        conn = _make_test_conn()
        _seed_person(conn, "Manny", props={"title": "Coach"})
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_properties_merge
            json.loads(graph_properties_merge(
                entity_type="Person", name="Manny",
                properties='{"title": "Head Coach"}',
            ))
        row = conn.execute("SELECT properties FROM nodes WHERE name='Manny'").fetchone()
        self.assertEqual(json.loads(row["properties"])["title"], "Head Coach")

    def test_merge_first_name_writes_to_column(self):
        conn = _make_test_conn()
        _seed_person(conn, "Manny", first_name=None, props={})
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_properties_merge
            json.loads(graph_properties_merge(
                entity_type="Person", name="Manny",
                properties='{"first_name": "Manny"}',
            ))
        row = conn.execute("SELECT first_name, properties FROM nodes WHERE name='Manny'").fetchone()
        self.assertEqual(row["first_name"], "Manny")
        # And first_name should NOT be in the properties blob (column-backed)
        self.assertNotIn("first_name", json.loads(row["properties"]))

    def test_merge_node_not_found(self):
        conn = _make_test_conn()
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_properties_merge
            result = json.loads(graph_properties_merge(
                entity_type="Person", name="DoesNotExist",
                properties='{"title": "x"}',
            ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "not_found")


class PropertiesRemoveTests(unittest.TestCase):
    def test_remove_only_named_keys(self):
        conn = _make_test_conn()
        _seed_person(conn, "Manny", props={"title": "Coach", "notes": "x", "aliases": ["Manny"]})
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_properties_remove
            result = json.loads(graph_properties_remove(
                entity_type="Person", name="Manny", keys=["notes"],
            ))
        self.assertTrue(result["ok"])
        row = conn.execute("SELECT properties FROM nodes WHERE name='Manny'").fetchone()
        props = json.loads(row["properties"])
        self.assertNotIn("notes", props)
        self.assertEqual(props["title"], "Coach")
        self.assertEqual(props["aliases"], ["Manny"])

    def test_remove_first_name_clears_column(self):
        conn = _make_test_conn()
        _seed_person(conn, "Manny", first_name="Manny", props={})
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_properties_remove
            json.loads(graph_properties_remove(
                entity_type="Person", name="Manny", keys=["first_name"],
            ))
        row = conn.execute("SELECT first_name FROM nodes WHERE name='Manny'").fetchone()
        self.assertIsNone(row["first_name"])

    def test_protected_metadata_rejected(self):
        """Identity / timestamp / staleness fields stay protected."""
        conn = _make_test_conn()
        _seed_person(conn, "Manny")
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_properties_remove
            result = json.loads(graph_properties_remove(
                entity_type="Person", name="Manny", keys=["name", "type"],
            ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "protected_metadata")

    def test_vector_leakage_can_be_stripped(self):
        """data_class / tier / source are vector-store fields and must be
        removable from KG nodes during the tidy walk."""
        conn = _make_test_conn()
        _seed_person(
            conn,
            "Manny",
            props={"data_class": "person", "tier": "durable", "source": "user"},
        )
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_properties_remove
            result = json.loads(graph_properties_remove(
                entity_type="Person",
                name="Manny",
                keys=["data_class", "tier", "source"],
            ))
        self.assertTrue(result["ok"])
        self.assertEqual(
            sorted(result["removed_keys"]),
            ["data_class", "source", "tier"],
        )


class EdgeCreateTests(unittest.TestCase):
    def test_edge_create_does_not_touch_source_props(self):
        """The regression test for the bug that bit Manny: creating an
        edge MUST NOT clobber the source node's properties."""
        conn = _make_test_conn()
        manny_id = _seed_person(conn, "Manny", props={"title": "Coach", "aliases": ["Manny"]})
        _seed_person(conn, "Zoe")
        before = conn.execute("SELECT properties FROM nodes WHERE id=?", (manny_id,)).fetchone()["properties"]
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_edge_create
            result = json.loads(graph_edge_create(
                source_name="Manny", source_type="Person",
                target_name="Zoe", target_type="Person",
                relation="PARENT_OF", data_class="current-state",
                mind_id="testmind",
            ))
        self.assertTrue(result["ok"])
        after = conn.execute("SELECT properties FROM nodes WHERE id=?", (manny_id,)).fetchone()["properties"]
        self.assertEqual(before, after)  # source properties untouched
        # Edge exists
        edge = conn.execute(
            "SELECT type FROM edges WHERE source_id=? AND target_id=?", (manny_id, manny_id+1)
        ).fetchone()
        self.assertEqual(edge["type"], "PARENT_OF")

    def test_edge_create_rejects_missing_source(self):
        conn = _make_test_conn()
        _seed_person(conn, "Target")
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_edge_create
            result = json.loads(graph_edge_create(
                source_name="Ghost", source_type="Person",
                target_name="Target", target_type="Person",
                relation="PARENT_OF", data_class="current-state",
                mind_id="testmind",
            ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "source_not_found")

    def test_edge_create_rejects_duplicate(self):
        conn = _make_test_conn()
        _seed_person(conn, "Manny")
        _seed_person(conn, "Zoe")
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_edge_create
            first = json.loads(graph_edge_create(
                source_name="Manny", source_type="Person",
                target_name="Zoe", target_type="Person",
                relation="PARENT_OF", data_class="current-state",
                mind_id="testmind",
            ))
            second = json.loads(graph_edge_create(
                source_name="Manny", source_type="Person",
                target_name="Zoe", target_type="Person",
                relation="PARENT_OF", data_class="current-state",
                mind_id="testmind",
            ))
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "edge_exists")


class EdgeDeleteTests(unittest.TestCase):
    def test_delete_by_tuple(self):
        conn = _make_test_conn()
        manny_id = _seed_person(conn, "Manny")
        zoe_id = _seed_person(conn, "Zoe")
        conn.execute(
            "INSERT INTO edges (mind_id, source_id, target_id, type, created_at) "
            "VALUES ('testmind', ?, ?, 'PARENT_OF', ?)",
            (manny_id, zoe_id, time.time()),
        )
        conn.commit()
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_edge_delete
            result = json.loads(graph_edge_delete(
                source_name="Manny", source_type="Person",
                target_name="Zoe", target_type="Person",
                relation="PARENT_OF",
            ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], 1)
        remaining = conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()
        self.assertEqual(remaining["c"], 0)


class NodeDeleteTests(unittest.TestCase):
    def test_delete_cascades_edges(self):
        conn = _make_test_conn()
        manny_id = _seed_person(conn, "Manny")
        zoe_id = _seed_person(conn, "Zoe")
        carl_id = _seed_person(conn, "Carl")
        now = time.time()
        # Manny has both inbound and outbound edges; both should be cascaded.
        conn.execute(
            "INSERT INTO edges (mind_id, source_id, target_id, type, created_at) "
            "VALUES ('testmind', ?, ?, 'PARENT_OF', ?)",
            (manny_id, zoe_id, now),
        )
        conn.execute(
            "INSERT INTO edges (mind_id, source_id, target_id, type, created_at) "
            "VALUES ('testmind', ?, ?, 'FRIEND_OF', ?)",
            (carl_id, manny_id, now),
        )
        conn.commit()
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_node_delete
            result = json.loads(graph_node_delete(entity_type="Person", name="Manny"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["node_deleted"], 1)
        self.assertEqual(result["edges_deleted"], 2)
        remaining_nodes = conn.execute(
            "SELECT COUNT(*) AS c FROM nodes WHERE name='Manny'"
        ).fetchone()
        self.assertEqual(remaining_nodes["c"], 0)
        # The other nodes (Zoe, Carl) survive.
        survivors = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()
        self.assertEqual(survivors["c"], 2)

    def test_delete_missing_node(self):
        conn = _make_test_conn()
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import graph_node_delete
            result = json.loads(graph_node_delete(entity_type="Person", name="Nobody"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
