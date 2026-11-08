"""Knowledge graph schema source of truth.

Hardcoded for now per design decision 2026-05-10. This file is what the
running system trusts at first init; the validator reads the seeded
schema_types / schema_edges tables thereafter.

Schema shape per type:
    name → {
        kind:     "first-class" | "second-class",
        required: [field names that callers must supply],
        optional: [field names callers may supply],
        enums:    { field_name: [allowed values] },
    }

Edges:
    name → {
        source_type:     <node type>,
        target_type:     <node type>,
        required_attrs:  [edge attr names],
        optional_attrs:  [edge attr names],
        enums:           { attr_name: [allowed values] },
        symmetric:       bool,
    }

`mind_id`, `id`, `created_at`, `updated_at` are infrastructure fields handled
by the write path — they are NOT user-supplied and NOT validated here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Mapping


SCHEMA_TYPES: dict[str, dict] = {
    # ---- First-class ----
    "Person": {
        "kind": "first-class",
        "required": ["name"],
        "optional": [
            "first_name",
            "middle_name",
            "last_name",
            "title",
            "aliases",
            "birthday",
            "phonetic",
        ],
        "enums": {},
    },
    "Mind": {
        "kind": "first-class",
        "required": ["name"],
        "optional": [],
        "enums": {},
    },
    "Device": {
        "kind": "first-class",
        "required": ["name", "kind"],
        "optional": ["purpose", "as_of", "config"],
        "enums": {
            "kind": ["phone", "laptop", "desktop", "server", "workstation", "tablet", "iot"],
        },
    },
    "Organization": {
        "kind": "first-class",
        "required": ["name"],
        "optional": ["full_name", "kind", "email", "phone", "website", "as_of"],
        "enums": {},
    },
    # ---- Person facets ----
    "ContactMethod": {
        "kind": "second-class",
        "required": ["kind", "value"],
        "optional": ["label", "as_of"],
        "enums": {
            "kind": ["email", "phone", "telegram", "discord", "slack", "mailing_address"],
            "label": ["work", "personal", "school", "planka"],
        },
    },
    "Place": {
        "kind": "second-class",
        "required": ["name", "kind"],
        "optional": ["as_of"],
        "enums": {
            "kind": ["home", "work", "school", "city", "country"],
        },
    },
    "Address": {
        "kind": "second-class",
        "required": ["street"],
        "optional": ["city", "state", "postal_code", "country", "label", "as_of"],
        "enums": {
            "label": ["home", "work", "mailing"],
        },
    },
    "Interest": {
        "kind": "second-class",
        "required": ["name", "kind"],
        "optional": ["notes", "as_of"],
        "enums": {
            "kind": ["hobby", "sport", "media", "topic", "food"],
        },
    },
    "HealthFact": {
        "kind": "second-class",
        "required": ["summary"],
        "optional": ["relevance", "as_of"],
        "enums": {
            "relevance": ["informational", "actionable", "dietary", "medication"],
        },
    },
    "Aspiration": {
        "kind": "second-class",
        "required": ["summary"],
        "optional": ["as_of"],
        "enums": {},
    },
    "Education": {
        "kind": "second-class",
        "required": ["level", "school_year"],
        "optional": ["school", "as_of"],
        "enums": {},
    },
    "Property": {
        "kind": "first-class",
        "required": ["name", "kind", "purpose", "status"],
        "optional": ["address", "as_of"],
        "enums": {
            "kind": ["residential", "commercial", "land", "mixed-use"],
            "purpose": ["primary", "vacation", "rental", "investment", "second-home"],
            "status": ["owned", "sold", "under-contract", "pending"],
        },
    },
    "LifeEvent": {
        "kind": "second-class",
        "required": ["name", "kind", "summary"],
        "optional": ["started_at", "ended_at", "as_of"],
        "enums": {
            "kind": ["military", "financial", "career", "personal", "move", "health", "award", "travel"],
        },
    },
    # ---- Device facets ----
    "OS": {
        "kind": "second-class",
        "required": ["name"],
        "optional": ["version", "as_of"],
        "enums": {},
    },
    "Software": {
        "kind": "second-class",
        "required": ["name"],
        "optional": ["purpose", "as_of"],
        "enums": {},
    },
}


SCHEMA_EDGES: dict[str, dict] = {
    "HAS_CONTACT": {
        "source_type": "Person",
        "target_type": "ContactMethod",
        "required_attrs": [],
        "optional_attrs": ["is_primary"],
        "enums": {},
        "symmetric": False,
    },
    "LIVES_IN": {
        "source_type": "Person",
        "target_type": "Place",
        "required_attrs": [],
        "optional_attrs": ["since"],
        "enums": {},
        "symmetric": False,
    },
    "HAS_ADDRESS": {
        "source_type": "Person",
        "target_type": "Address",
        "required_attrs": [],
        "optional_attrs": ["since", "until"],
        "enums": {},
        "symmetric": False,
    },
    "WORKS_IN": {
        "source_type": "Person",
        "target_type": "Place",
        "required_attrs": [],
        "optional_attrs": [],
        "enums": {},
        "symmetric": False,
    },
    "LOCATED_AT": {
        "source_type": "Organization",
        "target_type": "Place",
        "required_attrs": [],
        "optional_attrs": ["since", "until"],
        "enums": {},
        "symmetric": False,
    },
    "HAS_SIDE_GIG": {
        "source_type": "Person",
        "target_type": "Organization",
        "required_attrs": [],
        "optional_attrs": ["since", "until"],
        "enums": {},
        "symmetric": False,
    },
    "FOUNDED_BY": {
        "source_type": "Organization",
        "target_type": "Person",
        "required_attrs": [],
        "optional_attrs": ["since"],
        "enums": {},
        "symmetric": False,
    },
    "INTERESTED_IN": {
        "source_type": "Person",
        "target_type": "Interest",
        "required_attrs": [],
        "optional_attrs": ["strength", "since"],
        "enums": {},
        "symmetric": False,
    },
    "HAS_HEALTH_FACT": {
        "source_type": "Person",
        "target_type": "HealthFact",
        "required_attrs": [],
        "optional_attrs": [],
        "enums": {},
        "symmetric": False,
    },
    "ASPIRES_TO": {
        "source_type": "Person",
        "target_type": "Aspiration",
        "required_attrs": [],
        "optional_attrs": [],
        "enums": {},
        "symmetric": False,
    },
    "HAS_EDUCATION": {
        "source_type": "Person",
        "target_type": "Education",
        "required_attrs": [],
        "optional_attrs": [],
        "enums": {},
        "symmetric": False,
    },
    "PARENT_OF": {
        "source_type": "Person",
        "target_type": "Person",
        "required_attrs": [],
        "optional_attrs": [],
        "enums": {},
        "symmetric": False,
    },
    "CHILD_OF": {
        "source_type": "Person",
        "target_type": "Person",
        "required_attrs": [],
        "optional_attrs": [],
        "enums": {},
        "symmetric": False,
    },
    "SPOUSE_OF": {
        "source_type": "Person",
        "target_type": "Person",
        "required_attrs": [],
        "optional_attrs": ["since", "until"],
        "enums": {},
        "symmetric": True,
    },
    "SIBLING_OF": {
        "source_type": "Person",
        "target_type": "Person",
        "required_attrs": [],
        "optional_attrs": [],
        "enums": {},
        "symmetric": True,
    },
    "REPORTS_TO": {
        "source_type": "Person",
        "target_type": "Person",
        "required_attrs": [],
        "optional_attrs": ["since", "until"],
        "enums": {},
        "symmetric": False,
    },
    "FRIEND_OF": {
        "source_type": "Person",
        "target_type": "Person",
        "required_attrs": [],
        "optional_attrs": ["since", "context"],
        "enums": {},
        "symmetric": True,
    },
    "MEMBER_OF": {
        "source_type": "Person",
        "target_type": "Organization",
        "required_attrs": [],
        "optional_attrs": ["role", "title", "since", "until"],
        "enums": {},
        "symmetric": False,
    },
    # ---- Device edges ----
    "OWNS": {
        "source_type": "Person",
        "target_type": "Device",
        "required_attrs": [],
        "optional_attrs": ["since"],
        "enums": {},
        "symmetric": False,
    },
    "OWNS_PROPERTY": {
        "source_type": "Person",
        "target_type": "Property",
        "required_attrs": [],
        "optional_attrs": ["ownership_share", "acquired", "disposed"],
        "enums": {},
        "symmetric": False,
    },
    "EXPERIENCED": {
        "source_type": "Person",
        "target_type": "LifeEvent",
        "required_attrs": [],
        "optional_attrs": [],
        "enums": {},
        "symmetric": False,
    },
    "RUNS": {
        "source_type": "Device",
        "target_type": "OS",
        "required_attrs": [],
        "optional_attrs": ["since"],
        "enums": {},
        "symmetric": False,
    },
    "HAS_INSTALLED": {
        "source_type": "Device",
        "target_type": "Software",
        "required_attrs": [],
        "optional_attrs": ["since"],
        "enums": {},
        "symmetric": False,
    },
}




@dataclass(frozen=True)
class Registry:
    """Every registered node type and relationship, held as one value.

    The validators used to take a connection and run a SELECT per answer —
    one to find the spec, another to list the valid names for the refusal.
    That shape cannot survive the move off SQLite. An asyncpg connection has
    neither ``execute(...).fetchone()`` nor ``?`` placeholders, and making
    the validators ``async`` to reach it would make eighteen tests of pure
    validator logic — that ``ContactMethod`` requires a ``value``, that
    ``HAS_CONTACT`` runs Person → ContactMethod — need a database to check
    something no database decides.

    So the loading is one awaitable step, and ``validate_node`` and
    ``validate_edge`` are synchronous and pure over its result. They carry
    the four refusals the console renders, each with a payload (the valid
    types, the required fields, the allowed fields, the allowed values), and
    those are exactly the paths that must stay testable with no server.
    """

    types: Mapping[str, dict] = field(default_factory=dict)
    edges: Mapping[str, dict] = field(default_factory=dict)

    def type_spec(self, entity_type: str) -> dict | None:
        return self.types.get(entity_type)

    def edge_spec(self, relation: str) -> dict | None:
        return self.edges.get(relation)

    def type_names(self) -> list[str]:
        return sorted(self.types)

    def edge_names(self) -> list[str]:
        return sorted(self.edges)


def hardcoded_registry() -> Registry:
    """The registry this file declares, without reading the database.

    Not the runtime source of truth — ``load_registry`` is. This is what
    ``seed_schema_tables`` writes, and it is the same shape the loaded one
    has, which is what makes a seed-then-load test able to prove the round
    trip rather than just that both sides agree with themselves.
    """
    return Registry(types=dict(SCHEMA_TYPES), edges=dict(SCHEMA_EDGES))


def _edge_row_as_spec(row) -> dict:
    """One ``memory.schema_edges`` row in the shape ``SCHEMA_EDGES`` uses."""
    attr_schema = json.loads(row["attr_schema"])
    return {
        "source_type": row["source_type"],
        "target_type": row["target_type"],
        "required_attrs": attr_schema.get("required_attrs", []),
        "optional_attrs": attr_schema.get("optional_attrs", []),
        "enums": attr_schema.get("enums", {}),
        "symmetric": bool(row["symmetric"]),
    }


async def load_registry() -> Registry:
    """Read the registry the running system trusts, in two queries.

    Two queries for the whole registry rather than one per refusal: the
    strict upsert route validates a node and then an edge, and the old shape
    spent up to six round trips doing it. Against a file that cost nothing.
    """
    from hive_data import graph

    types = {
        row["name"]: json.loads(row["property_schema"])
        for row in await graph.types_all()
    }
    edges = {
        row["name"]: _edge_row_as_spec(row)
        for row in await graph.edge_types_all()
    }
    return Registry(types=types, edges=edges)


async def seed_schema_tables() -> dict[str, int]:
    """Write the declared registry into the two schema tables.

    Mandatory, not a first-init convenience. ``memory.nodes.type`` is a real
    foreign key into ``memory.schema_types`` ON DELETE RESTRICT, and SQLite
    never enforced it because lucent's connection never set the pragma — so
    on any database that is not the final load (a fresh install, CI, a dev
    box) an unseeded registry makes every write refuse with
    ``Must be one of: []``.

    Types before relationships: ``schema_edges.source_type`` and
    ``target_type`` are foreign keys into ``schema_types`` too, so a
    relationship seeded first names a type that is not there yet.

    Idempotent through ``ON CONFLICT DO NOTHING``, which is what
    ``INSERT OR IGNORE`` was and what Postgres does not have. Rows added
    later through the admin routes are left exactly as they are.

    Reports counts rather than returning nothing, so a seed that wrote
    nothing is distinguishable from one that never ran.
    """
    from hive_data import graph

    now = time.time()
    types = 0
    edges = 0

    for name, spec in SCHEMA_TYPES.items():
        written = await graph.type_put(
            name=name, kind=spec["kind"], property_schema=spec,
            created_at=now)
        types += int(written.get("rows") or 0)

    for name, spec in SCHEMA_EDGES.items():
        written = await graph.edge_type_put(
            name=name, source_type=spec["source_type"],
            target_type=spec["target_type"], attr_schema=spec,
            symmetric=1 if spec["symmetric"] else 0, created_at=now)
        edges += int(written.get("rows") or 0)

    return {"types": types, "edges": edges}


def validate_node(
    entity_type: str, properties: dict, *, registry: Registry
) -> tuple[bool, str | None, dict | None]:
    """Validate a node write against the registry.

    Synchronous and pure: everything it needs is in ``registry``, which the
    caller loaded. The hardcoded ``SCHEMA_TYPES`` dict is what
    ``seed_schema_tables`` writes, never what this reads.

    ``registry`` is keyword-only and has no default. It used to default to
    the lazy-singleton connection, and a default here would be the one thing
    worse than a missing argument: a validator that quietly checks against
    an empty registry refuses every type, and the refusal reads like the
    caller's fault.

    Returns (ok, code, detail_dict). On failure, code is one of:
        unknown_node_type, missing_required, unknown_field, invalid_enum
    On success returns (True, None, None).
    """
    spec = registry.type_spec(entity_type)
    if spec is None:
        return False, "unknown_node_type", {
            "detail": f"entity_type {entity_type!r} is not in the schema allow-list",
            "valid_types": registry.type_names(),
        }

    required = list(spec.get("required") or [])
    allowed = set(required) | set(spec.get("optional") or [])
    # Identity / infrastructure fields tolerated and ignored by validation —
    # the write path owns them.
    INFRA = {"id", "mind_id", "created_at", "updated_at", "type", "name"}

    for field_name in required:
        if field_name == "name":
            # name is column-level on the node row; required for all types.
            continue
        if field_name not in properties:
            return False, "missing_required", {
                "detail": f"property {field_name!r} is required for {entity_type}",
                "required": required,
            }

    for field_name in properties:
        if field_name in INFRA:
            continue
        if field_name not in allowed:
            return False, "unknown_field", {
                "detail": f"property {field_name!r} is not part of the {entity_type} schema",
                "allowed": sorted(allowed),
            }

    for field_name, allowed_values in (spec.get("enums") or {}).items():
        if field_name in properties and properties[field_name] not in allowed_values:
            return False, "invalid_enum", {
                "detail": f"{field_name}={properties[field_name]!r} not in allowed values for {entity_type}",
                "allowed_values": allowed_values,
            }

    return True, None, None


def validate_edge(
    relation: str,
    source_type: str,
    target_type: str,
    attrs: dict | None = None,
    *,
    registry: Registry,
) -> tuple[bool, str | None, dict | None]:
    """Validate an edge write against the registry.

    Synchronous and pure, for the same reason ``validate_node`` is.

    Returns (ok, code, detail_dict). On failure, code is one of:
        unknown_edge_type, invalid_edge_direction, missing_required_attr,
        unknown_attr, invalid_enum
    """
    attrs = attrs or {}

    spec = registry.edge_spec(relation)
    if spec is None:
        return False, "unknown_edge_type", {
            "detail": f"relation {relation!r} is not in the schema allow-list",
            "valid_edges": registry.edge_names(),
        }

    if source_type != spec["source_type"] or target_type != spec["target_type"]:
        return False, "invalid_edge_direction", {
            "detail": (
                f"edge {relation} requires {spec['source_type']} → "
                f"{spec['target_type']}, got {source_type} → {target_type}"
            ),
            "expected_source": spec["source_type"],
            "expected_target": spec["target_type"],
        }

    required_attrs = list(spec.get("required_attrs") or [])
    allowed_attrs = set(required_attrs) | set(spec.get("optional_attrs") or [])

    for attr in required_attrs:
        if attr not in attrs:
            return False, "missing_required_attr", {
                "detail": f"attr {attr!r} is required for {relation}",
                "required_attrs": required_attrs,
            }

    for attr in attrs:
        if attr not in allowed_attrs:
            return False, "unknown_attr", {
                "detail": f"attr {attr!r} is not part of the {relation} schema",
                "allowed": sorted(allowed_attrs),
            }

    for attr, allowed_values in (spec.get("enums") or {}).items():
        if attr in attrs and attrs[attr] not in allowed_values:
            return False, "invalid_enum", {
                "detail": f"{attr}={attrs[attr]!r} not in allowed values for {relation}",
                "allowed_values": allowed_values,
            }

    return True, None, None
