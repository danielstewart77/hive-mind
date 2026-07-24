"""Tests for audit_person_nodes and update_person_names.

These helpers used to live in skippy's tools/stateful/knowledge_graph.py
(deleted in skippy Phase B3). The live versions are here in NS at
``lucent_api/lucent_graph.py``. Tests follow the in-memory SQLite +
patched ``_get_conn`` pattern from ``test_graph_query_mind_id.py`` —
exercises the real SQL against the real schema, no Neo4j mocking.

Run: python -m unittest lucent_api.tests.test_person_node_audit
"""

from __future__ import annotations

import json
import sqlite3
import time
import unittest
from unittest.mock import patch


def _make_test_conn() -> sqlite3.Connection:
    """In-memory SQLite with the minimal nodes schema audit/update need."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
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
        """
    )
    return conn


def _insert_person(
    conn: sqlite3.Connection,
    *,
    name: str,
    mind_id: str,
    first_name: str | None,
    last_name: str | None,
    properties: dict | None = None,
) -> int:
    now = time.time()
    cursor = conn.execute(
        """
        INSERT INTO nodes
            (mind_id, type, name, first_name, last_name, properties,
             data_class, tier, created_at)
        VALUES (?, 'Person', ?, ?, ?, ?, 'current-state', 'contextual', ?)
        """,
        (
            mind_id,
            name,
            first_name,
            last_name,
            json.dumps(properties or {}),
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid


class AuditPersonNodesTests(unittest.TestCase):
    """``audit_person_nodes`` returns Person rows missing either name part."""

    def test_returns_nodes_missing_first_name(self) -> None:
        conn = _make_test_conn()
        _insert_person(conn, name="David Stewart", mind_id="ada",
                       first_name=None, last_name="Stewart")
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import audit_person_nodes
            result = json.loads(audit_person_nodes(mind_id="ada"))
        self.assertTrue(result["found"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["nodes"][0]["name"], "David Stewart")
        self.assertIsNone(result["nodes"][0]["first_name"])

    def test_returns_nodes_missing_last_name(self) -> None:
        conn = _make_test_conn()
        _insert_person(conn, name="Jane", mind_id="ada",
                       first_name="Jane", last_name=None)
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import audit_person_nodes
            result = json.loads(audit_person_nodes(mind_id="ada"))
        self.assertTrue(result["found"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["nodes"][0]["name"], "Jane")
        self.assertIsNone(result["nodes"][0]["last_name"])

    def test_excludes_complete_nodes(self) -> None:
        conn = _make_test_conn()
        _insert_person(conn, name="Daniel Stewart", mind_id="ada",
                       first_name="Daniel", last_name="Stewart")
        _insert_person(conn, name="Sloan", mind_id="ada",
                       first_name=None, last_name=None)
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import audit_person_nodes
            result = json.loads(audit_person_nodes(mind_id="ada"))
        self.assertTrue(result["found"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["nodes"][0]["name"], "Sloan")

    def test_returns_empty_when_all_complete(self) -> None:
        conn = _make_test_conn()
        _insert_person(conn, name="Daniel Stewart", mind_id="ada",
                       first_name="Daniel", last_name="Stewart")
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import audit_person_nodes
            result = json.loads(audit_person_nodes(mind_id="ada"))
        self.assertFalse(result["found"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["nodes"], [])

    def test_isolates_by_mind_id(self) -> None:
        """Only the requested mind's incomplete nodes are returned."""
        conn = _make_test_conn()
        _insert_person(conn, name="Ada Person", mind_id="ada",
                       first_name=None, last_name="Person")
        _insert_person(conn, name="Bob Person", mind_id="bob",
                       first_name=None, last_name="Person")
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import audit_person_nodes
            result = json.loads(audit_person_nodes(mind_id="ada"))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["nodes"][0]["name"], "Ada Person")

    def test_includes_properties_blob(self) -> None:
        conn = _make_test_conn()
        _insert_person(conn, name="Maurice", mind_id="ada",
                       first_name=None, last_name="X",
                       properties={"church": "Anchor Bend"})
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import audit_person_nodes
            result = json.loads(audit_person_nodes(mind_id="ada"))
        self.assertEqual(result["count"], 1)
        props = result["nodes"][0]["properties"]
        self.assertEqual(props.get("church"), "Anchor Bend")


class UpdatePersonNamesTests(unittest.TestCase):
    """``update_person_names`` backfills first_name and/or last_name."""

    def test_sets_first_and_last_name(self) -> None:
        conn = _make_test_conn()
        _insert_person(conn, name="David Stewart", mind_id="ada",
                       first_name=None, last_name=None)
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import update_person_names
            result = json.loads(update_person_names(
                name="David Stewart",
                first_name="David",
                last_name="Stewart",
                mind_id="ada",
            ))
        self.assertTrue(result["updated"])
        self.assertEqual(result["first_name"], "David")
        self.assertEqual(result["last_name"], "Stewart")
        # Read back and verify SQL UPDATE actually landed.
        row = conn.execute(
            "SELECT first_name, last_name FROM nodes WHERE name = ?",
            ("David Stewart",),
        ).fetchone()
        self.assertEqual(row["first_name"], "David")
        self.assertEqual(row["last_name"], "Stewart")

    def test_allows_partial_update_first_name_only(self) -> None:
        conn = _make_test_conn()
        _insert_person(conn, name="Jane Doe", mind_id="ada",
                       first_name=None, last_name="Doe")
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import update_person_names
            result = json.loads(update_person_names(
                name="Jane Doe",
                first_name="Jane",
                mind_id="ada",
            ))
        self.assertTrue(result["updated"])
        self.assertEqual(result["first_name"], "Jane")
        self.assertNotIn("last_name", result)
        row = conn.execute(
            "SELECT first_name, last_name FROM nodes WHERE name = ?",
            ("Jane Doe",),
        ).fetchone()
        self.assertEqual(row["first_name"], "Jane")
        self.assertEqual(row["last_name"], "Doe")  # untouched

    def test_requires_at_least_one_name_field(self) -> None:
        with patch("lucent_api.lucent_graph._get_conn", return_value=_make_test_conn()):
            from lucent_api.lucent_graph import update_person_names
            result = json.loads(update_person_names(
                name="X",
                mind_id="ada",
            ))
        self.assertIn("error", result)
        self.assertIn("first_name", result["error"])
        self.assertIn("last_name", result["error"])

    def test_node_not_found_returns_updated_false(self) -> None:
        conn = _make_test_conn()
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import update_person_names
            result = json.loads(update_person_names(
                name="Nonexistent",
                first_name="X",
                last_name="Y",
                mind_id="ada",
            ))
        self.assertFalse(result["updated"])
        self.assertIn("reason", result)

    def test_isolates_by_mind_id_on_update(self) -> None:
        """An update for mind_id=ada must not touch a same-named bob row."""
        conn = _make_test_conn()
        _insert_person(conn, name="Twin", mind_id="ada",
                       first_name=None, last_name=None)
        _insert_person(conn, name="Twin", mind_id="bob",
                       first_name="OriginalBob", last_name="Surname")
        with patch("lucent_api.lucent_graph._get_conn", return_value=conn):
            from lucent_api.lucent_graph import update_person_names
            result = json.loads(update_person_names(
                name="Twin",
                first_name="UpdatedAda",
                last_name="Smith",
                mind_id="ada",
            ))
        self.assertTrue(result["updated"])
        ada_row = conn.execute(
            "SELECT first_name FROM nodes WHERE name = 'Twin' AND mind_id = 'ada'"
        ).fetchone()
        bob_row = conn.execute(
            "SELECT first_name FROM nodes WHERE name = 'Twin' AND mind_id = 'bob'"
        ).fetchone()
        self.assertEqual(ada_row["first_name"], "UpdatedAda")
        self.assertEqual(bob_row["first_name"], "OriginalBob")


if __name__ == "__main__":
    unittest.main()
