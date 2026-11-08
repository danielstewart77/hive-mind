"""The validators, and the seed they validate against.

The validators used to take a connection and run a SELECT per answer. They
now take a loaded `Registry` and run no query at all, which is what lets the
18 cases below — that `ContactMethod` requires a `value`, that `HAS_CONTACT`
runs Person → ContactMethod, that `fax` is not a contact kind — keep running
with no database anywhere. None of those facts is a database's to decide, and
making them `async` to reach one would have made eighteen pure assertions
depend on a Postgres being up.

What *does* need the database is the other half, and it is new: the seed is
mandatory now. `memory.nodes.type` is a real foreign key into
`memory.schema_types` ON DELETE RESTRICT, which SQLite never enforced because
lucent's connection never set the pragma. On a fresh install, in CI, on a dev
box — anywhere that is not the final load — an unseeded registry refuses
every write with `Must be one of: []`. The three tests at the bottom drive
the seed against a real database and skip, cleanly, where there is none.
"""

from __future__ import annotations

import json
import unittest

import pytest

from lucent_api.schema_registry import (
    SCHEMA_EDGES,
    SCHEMA_TYPES,
    hardcoded_registry,
    load_registry,
    seed_schema_tables,
    validate_edge,
    validate_node,
)


class ValidateNodeTests(unittest.TestCase):
    """No fixture, no connection, no event loop. That is the point."""

    def setUp(self):
        self.registry = hardcoded_registry()

    def test_valid_person_passes(self):
        ok, code, detail = validate_node(
            "Person", {"first_name": "Daniel"}, registry=self.registry)
        self.assertTrue(ok)
        self.assertIsNone(code)
        self.assertIsNone(detail)

    def test_valid_contactmethod_passes(self):
        ok, code, _ = validate_node(
            "ContactMethod", {"kind": "email", "value": "x@y.z", "label": "work"},
            registry=self.registry,
        )
        self.assertTrue(ok)
        self.assertIsNone(code)

    def test_unknown_type_rejected(self):
        ok, code, detail = validate_node("Project", {}, registry=self.registry)
        self.assertFalse(ok)
        self.assertEqual(code, "unknown_node_type")
        self.assertIn("Person", detail["valid_types"])

    def test_missing_required_property_rejected(self):
        # ContactMethod requires both 'kind' and 'value'
        ok, code, _ = validate_node(
            "ContactMethod", {"kind": "email"}, registry=self.registry)
        self.assertFalse(ok)
        self.assertEqual(code, "missing_required")

    def test_unknown_field_rejected(self):
        ok, code, _ = validate_node(
            "Person", {"first_name": "Daniel", "favorite_color": "orange"},
            registry=self.registry,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "unknown_field")

    def test_invalid_enum_value_rejected(self):
        ok, code, detail = validate_node(
            "ContactMethod", {"kind": "fax", "value": "555-1234"},
            registry=self.registry,
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
            registry=self.registry,
        )
        self.assertTrue(ok)
        self.assertIsNone(code)

    def test_optional_fields_accepted(self):
        # Aspiration.as_of is optional — present should work, absent should
        # also work because it's not in 'required'.
        ok, _, _ = validate_node(
            "Aspiration", {"summary": "become a chef"}, registry=self.registry)
        self.assertTrue(ok)
        ok, _, _ = validate_node(
            "Aspiration", {"summary": "become a chef", "as_of": "2026-05-11"},
            registry=self.registry,
        )
        self.assertTrue(ok)

    def test_valid_education_passes(self):
        ok, _, _ = validate_node(
            "Education", {"level": "4th grade", "school_year": "2025-2026"},
            registry=self.registry,
        )
        self.assertTrue(ok)

    def test_education_missing_required_rejected(self):
        ok, code, _ = validate_node(
            "Education", {"level": "4th grade"}, registry=self.registry)
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
            registry=self.registry,
        )
        self.assertTrue(ok)


class ValidateEdgeTests(unittest.TestCase):
    def setUp(self):
        self.registry = hardcoded_registry()

    def test_valid_edge_passes(self):
        ok, code, _ = validate_edge(
            "HAS_CONTACT", "Person", "ContactMethod", registry=self.registry)
        self.assertTrue(ok)
        self.assertIsNone(code)

    def test_unknown_edge_type_rejected(self):
        ok, code, detail = validate_edge(
            "TOTALLY_BOGUS_REL", "Person", "Person", registry=self.registry)
        self.assertFalse(ok)
        self.assertEqual(code, "unknown_edge_type")
        self.assertIn("HAS_CONTACT", detail["valid_edges"])

    def test_wrong_direction_rejected(self):
        # HAS_CONTACT goes Person → ContactMethod, not the reverse
        ok, code, detail = validate_edge(
            "HAS_CONTACT", "ContactMethod", "Person", registry=self.registry)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_edge_direction")
        self.assertEqual(detail["expected_source"], "Person")
        self.assertEqual(detail["expected_target"], "ContactMethod")

    def test_self_referential_edge_passes(self):
        # SPOUSE_OF is Person → Person (symmetric, but direction still valid)
        ok, _, _ = validate_edge(
            "SPOUSE_OF", "Person", "Person", registry=self.registry)
        self.assertTrue(ok)

    def test_unknown_attr_rejected(self):
        ok, code, _ = validate_edge(
            "HAS_CONTACT", "Person", "ContactMethod",
            attrs={"weird_attr": True}, registry=self.registry,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "unknown_attr")

    def test_has_education_edge_passes(self):
        ok, _, _ = validate_edge(
            "HAS_EDUCATION", "Person", "Education", registry=self.registry)
        self.assertTrue(ok)

    def test_has_education_wrong_direction_rejected(self):
        ok, code, _ = validate_edge(
            "HAS_EDUCATION", "Education", "Person", registry=self.registry)
        self.assertFalse(ok)
        self.assertEqual(code, "invalid_edge_direction")


# ---- The seed, against a real database --------------------------------


@pytest.mark.asyncio
async def test_seeding_registers_every_declared_node_type(store, inspector):
    """Without this the foreign key refuses every node write.

    Read back with a plain driver connection rather than through
    `load_registry`, which is the module that would also be wrong if the
    encoding were.
    """
    await seed_schema_tables()

    registered = {
        row["name"]: row["property_schema"]
        for row in await inspector.fetch(
            'SELECT "name", "property_schema" FROM memory.schema_types')
    }

    assert set(SCHEMA_TYPES) <= set(registered)
    assert json.loads(registered["ContactMethod"])["required"] == ["kind", "value"]


@pytest.mark.asyncio
async def test_seeding_registers_relationships_after_the_types_they_name(
        store, inspector):
    """`schema_edges.source_type` is a foreign key into `schema_types`.

    So a relationship seeded before its endpoints names a row that is not
    there yet and the whole seed dies on the first edge. Nothing in the
    SQLite original enforced the order.
    """
    await seed_schema_tables()

    row = await inspector.fetchrow(
        'SELECT "source_type", "target_type", "attr_schema", "symmetric" '
        'FROM memory.schema_edges WHERE "name" = $1', "HAS_CONTACT")

    assert row is not None
    assert (row["source_type"], row["target_type"]) == ("Person", "ContactMethod")
    assert set(SCHEMA_EDGES) <= {
        r["name"] for r in await inspector.fetch(
            'SELECT "name" FROM memory.schema_edges')}


@pytest.mark.asyncio
async def test_a_second_seed_leaves_one_row_per_name(store, inspector):
    """`INSERT OR IGNORE` has no Postgres spelling, and the seed runs often.

    Every start-up seeds, and so does every test that writes a node. A
    plain insert on the second pass raises a duplicate key and takes the
    whole process down on the one path a fresh install cannot skip.
    """
    await seed_schema_tables()
    await seed_schema_tables()

    assert await inspector.fetchval(
        'SELECT COUNT(*) FROM memory.schema_types WHERE "name" = $1',
        "Person") == 1
    assert await inspector.fetchval(
        'SELECT COUNT(*) FROM memory.schema_edges WHERE "name" = $1',
        "HAS_CONTACT") == 1


@pytest.mark.asyncio
async def test_a_loaded_registry_carries_the_specs_the_seed_wrote(store):
    """The round trip, which neither half can prove alone.

    `hardcoded_registry` and the seed both read `SCHEMA_TYPES`, so a test
    using one to check the other agrees with itself whatever the encoding
    does. This loads the registry the running system actually validates
    against, out of the database, and then asks it for a refusal that can
    only come from the enum surviving the trip.
    """
    await seed_schema_tables()

    registry = await load_registry()

    ok, code, detail = validate_node(
        "ContactMethod", {"kind": "fax", "value": "555-1234"}, registry=registry)
    assert (ok, code) == (False, "invalid_enum")
    assert "telegram" in detail["allowed_values"]

    ok, code, detail = validate_edge(
        "HAS_CONTACT", "ContactMethod", "Person", registry=registry)
    assert (ok, code) == (False, "invalid_edge_direction")
    assert detail["expected_target"] == "ContactMethod"
