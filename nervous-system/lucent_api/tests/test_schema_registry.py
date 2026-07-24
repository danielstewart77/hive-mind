"""Unit tests for lucent_api.schema_registry validators.

Validators read their schema from the schema_types / schema_edges DB
tables (single runtime source of truth). These tests bring up an
in-memory SQLite seeded from the hardcoded ``SCHEMA_TYPES`` /
``SCHEMA_EDGES`` dicts and pass the connection in explicitly via the
``conn=`` parameter — no global singleton dependency.

Covers the five validations the strict upsert endpoint runs before
delegating to the write path: type allow-list, required properties,
unknown fields, enum values, edge type allow-list, edge direction.

Run: python -m unittest lucent_api.tests.test_schema_registry
"""

from __future__ import annotations

import sqlite3
import unittest

from lucent_api.schema_registry import (
    seed_schema_tables,
    validate_edge,
    validate_node,
)


def _make_seeded_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_types (
            name             TEXT    PRIMARY KEY,
            kind             TEXT    NOT NULL CHECK (kind IN ('first-class','second-class')),
            property_schema  TEXT    NOT NULL,
            created_at       REAL    NOT NULL
        );
        CREATE TABLE schema_edges (
            name             TEXT    PRIMARY KEY,
            source_type      TEXT    NOT NULL,
            target_type      TEXT    NOT NULL,
            attr_schema      TEXT    NOT NULL,
            symmetric        INTEGER NOT NULL DEFAULT 0,
            created_at       REAL    NOT NULL
        );
    """)
    seed_schema_tables(conn)
    conn.commit()
    return conn


class ValidateNodeTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_seeded_conn()

    def tearDown(self):
        self.conn.close()

    def test_valid_person_passes(self):
        ok, code, detail = validate_node("Person", {"first_name": "Daniel"}, conn=self.conn)
        self.assertTrue(ok)
        self.assertIsNone(code)
        self.assertIsNone(detail)

    def test_valid_contactmethod_passes(self):
        ok, code, _ = validate_node(
            "ContactMethod", {"kind": "email", "value": "x@y.z", "label": "work"},
            conn=self.conn,
        )
        self.assertTrue(ok)
        self.assertIsNone(code)

    def test_unknown_type_rejected(self):
        ok, code, detail = validate_node("Project", {}, conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(code, "unknown_node_type")
        self.assertIn("Person", detail["valid_types"])

    def test_missing_required_property_rejected(self):
        # ContactMethod requires both 'kind' and 'value'
        ok, code, _ = validate_node("ContactMethod", {"kind": "email"}, conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(code, "missing_required")

    def test_unknown_field_rejected(self):
        ok, code, _ = validate_node(
            "Person", {"first_name": "Daniel", "favorite_color": "orange"},
            conn=self.conn,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "unknown_field")

    def test_invalid_enum_value_rejected(self):
        ok, code, detail = validate_node(
            "ContactMethod", {"kind": "fax", "value": "555-1234"},
            conn=self.conn,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_enum")
        self.assertIn("email", detail["allowed_values"])

    def test_infrastructure_fields_ignored(self):
        # id, mind_id, created_at, updated_at, type, name are infrastructure
        # — caller may pass them but they don't fail "unknown_field".
        ok, code, _ = validate_node(
            "Person",
            {
                "first_name": "Daniel",
                "mind_id": "skippy",
                "created_at": "2026-05-11",
                "type": "Person",
            },
            conn=self.conn,
        )
        self.assertTrue(ok)
        self.assertIsNone(code)

    def test_optional_fields_accepted(self):
        # Aspiration.as_of is optional — present should work, absent should
        # also work because it's not in 'required'.
        ok, _, _ = validate_node("Aspiration", {"summary": "become a chef"}, conn=self.conn)
        self.assertTrue(ok)
        ok, _, _ = validate_node(
            "Aspiration", {"summary": "become a chef", "as_of": "2026-05-11"},
            conn=self.conn,
        )
        self.assertTrue(ok)

    def test_valid_education_passes(self):
        ok, _, _ = validate_node(
            "Education", {"level": "4th grade", "school_year": "2025-2026"},
            conn=self.conn,
        )
        self.assertTrue(ok)

    def test_education_missing_required_rejected(self):
        ok, code, _ = validate_node("Education", {"level": "4th grade"}, conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(code, "missing_required")

    def test_person_extended_whitelist(self):
        ok, _, _ = validate_node(
            "Person",
            {
                "first_name": "Daniel",
                "middle_name": "Sloan",
                "last_name": "Stewart",
                "title": "Dr.",
                "aliases": ["Sloan"],
                "birthday": "2014-04-03",
                "phonetic": "SHAO-lan",
            },
            conn=self.conn,
        )
        self.assertTrue(ok)


class ValidateEdgeTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_seeded_conn()

    def tearDown(self):
        self.conn.close()

    def test_valid_edge_passes(self):
        ok, code, _ = validate_edge("HAS_CONTACT", "Person", "ContactMethod", conn=self.conn)
        self.assertTrue(ok)
        self.assertIsNone(code)

    def test_unknown_edge_type_rejected(self):
        ok, code, detail = validate_edge("TOTALLY_BOGUS_REL", "Person", "Person", conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(code, "unknown_edge_type")
        self.assertIn("HAS_CONTACT", detail["valid_edges"])

    def test_wrong_direction_rejected(self):
        # HAS_CONTACT goes Person → ContactMethod, not the reverse
        ok, code, detail = validate_edge("HAS_CONTACT", "ContactMethod", "Person", conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_edge_direction")
        self.assertEqual(detail["expected_source"], "Person")
        self.assertEqual(detail["expected_target"], "ContactMethod")

    def test_self_referential_edge_passes(self):
        # SPOUSE_OF is Person → Person (symmetric, but direction still valid)
        ok, _, _ = validate_edge("SPOUSE_OF", "Person", "Person", conn=self.conn)
        self.assertTrue(ok)

    def test_unknown_attr_rejected(self):
        ok, code, _ = validate_edge(
            "HAS_CONTACT", "Person", "ContactMethod", attrs={"weird_attr": True},
            conn=self.conn,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "unknown_attr")

    def test_has_education_edge_passes(self):
        ok, _, _ = validate_edge("HAS_EDUCATION", "Person", "Education", conn=self.conn)
        self.assertTrue(ok)

    def test_has_education_wrong_direction_rejected(self):
        ok, code, _ = validate_edge("HAS_EDUCATION", "Education", "Person", conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_edge_direction")


if __name__ == "__main__":
    unittest.main()
