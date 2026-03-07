# Implementation Plan: 1723685509068228112 - [Memory] Schema & Metadata -- Foundation

## Overview

This story adds a data classification model to the memory subsystem, ensuring every `memory_store` and `graph_upsert` call carries structured metadata (`data_class`, `tier`, `as_of`, `expires_at`, `source`, `superseded`). A class registry constant defines the seven known data classes from `specs/memory-lifecycle.md`. Unknown classes are rejected with a prompt for Daniel. Neo4j indexes are created for the new metadata fields to support future pruning queries.

## Technical Approach

The class registry is defined as a Python dict constant in `agents/memory.py`, mapping each class name to its tier and tags. Validation logic lives in a new shared module `core/memory_schema.py` to avoid circular dependencies and keep both `agents/memory.py` and `agents/knowledge_graph.py` DRY. Both `memory_store`/`memory_store_direct` and `graph_upsert`/`graph_upsert_direct` gain the new parameters. During the transition period, `data_class` is optional with a deprecation warning logged when omitted (AC-8). Existing Neo4j entries are untouched (AC-7).

The epilogue processor (`core/epilogue.py`) currently calls `memory_store_direct` and `graph_upsert_direct` without `data_class` -- it will continue to work during the optional phase (deprecation warning), and a follow-up backfill story will update it to pass `data_class`.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|-------------|-------|
| MCP tool with validation | `/usr/src/app/agents/knowledge_graph.py` | `_validate_label()` pattern for validating against a set of allowed values |
| Unit test with classes | `/usr/src/app/tests/unit/test_audit.py` | Test class structure, pytest fixtures, mock patterns |
| Unit test for validation | `/usr/src/app/tests/unit/test_path_validation.py` | Testing validation with accepted and rejected inputs |
| Integration test pattern | `/usr/src/app/tests/integration/test_epilogue_processor.py` | Mock DB, mock gateway, patching imports |
| Conftest mock modules | `/usr/src/app/tests/unit/conftest.py` | Third-party module mocking for test isolation |

## Models & Schemas

### `core/memory_schema.py` (new file)

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class DataClassDef:
    """Definition of a memory data class."""
    name: str           # e.g. "technical-config"
    tier: str           # "reviewable" or "durable"
    tags: list[str]     # e.g. ["reviewable", "technical"]
    requires_expires: bool  # True only for timed-event

DATA_CLASS_REGISTRY: dict[str, DataClassDef] = {
    "technical-config": DataClassDef("technical-config", "reviewable", ["reviewable", "technical"], False),
    "session-log":      DataClassDef("session-log", "reviewable", ["reviewable", "session"], False),
    "timed-event":      DataClassDef("timed-event", "reviewable", ["reviewable", "event"], True),
    "person":           DataClassDef("person", "durable", ["durable", "person"], False),
    "world-event":      DataClassDef("world-event", "reviewable", ["reviewable", "world-event"], False),
    "preference":       DataClassDef("preference", "durable", ["durable", "preference"], False),
    "intention":        DataClassDef("intention", "reviewable", ["reviewable", "intention"], False),
}

VALID_SOURCES = {"user", "tool", "session", "self"}
VALID_TIERS = {"reviewable", "durable"}
```

### Metadata fields added to Neo4j nodes

On `Memory` nodes (vector store):
- `data_class: str` -- the class name
- `tier: str` -- "reviewable" or "durable"
- `as_of: str` -- ISO datetime
- `expires_at: str | None` -- ISO datetime, only for timed-event
- `source: str` -- "user", "tool", "session", or "self"
- `superseded: bool` -- default False

On knowledge graph nodes (all entity types):
- Same fields: `data_class`, `tier`, `as_of`, `source`, `superseded`

## Implementation Steps

### Step 1: Define the data class registry and validation functions

**Files:**
- Create: `core/memory_schema.py` -- data class registry, validation functions, tier derivation

**Test First (unit):** `tests/unit/test_memory_schema.py`
- [ ] `test_registry_contains_all_seven_classes` -- asserts DATA_CLASS_REGISTRY has exactly 7 entries with expected names
- [ ] `test_registry_technical_config_is_reviewable` -- asserts tier == "reviewable" for technical-config
- [ ] `test_registry_person_is_durable` -- asserts tier == "durable" for person
- [ ] `test_registry_timed_event_requires_expires` -- asserts requires_expires == True only for timed-event
- [ ] `test_validate_data_class_known_returns_def` -- asserts validate_data_class("person") returns the DataClassDef
- [ ] `test_validate_data_class_unknown_raises` -- asserts validate_data_class("unknown-class") raises ValueError with prompt message
- [ ] `test_validate_data_class_none_warns` -- asserts validate_data_class(None) returns None and logs a deprecation warning
- [ ] `test_validate_source_valid` -- asserts validate_source("user") passes
- [ ] `test_validate_source_invalid_raises` -- asserts validate_source("random") raises ValueError
- [ ] `test_build_metadata_with_data_class` -- asserts build_metadata(data_class="person", source="user") returns dict with tier="durable", as_of set, superseded=False, no expires_at
- [ ] `test_build_metadata_timed_event_requires_expires_at` -- asserts build_metadata(data_class="timed-event", source="user") raises ValueError when expires_at not provided
- [ ] `test_build_metadata_timed_event_with_expires_at` -- asserts build_metadata(data_class="timed-event", source="user", expires_at="2026-04-01T00:00:00Z") includes expires_at
- [ ] `test_build_metadata_without_data_class_returns_minimal` -- asserts build_metadata(data_class=None, source="user") returns dict with source and as_of but no tier/data_class
- [ ] `test_build_metadata_as_of_defaults_to_now` -- asserts as_of is close to current time when not explicitly provided
- [ ] `test_build_metadata_as_of_custom` -- asserts as_of uses provided value when given

**Then Implement:**
- [ ] Create `core/memory_schema.py` with `DataClassDef` dataclass, `DATA_CLASS_REGISTRY` dict (7 entries), `VALID_SOURCES`, `VALID_TIERS`
- [ ] Implement `validate_data_class(data_class: str | None) -> DataClassDef | None` -- returns DataClassDef for known classes, raises ValueError with prompt message for unknown non-None values, logs deprecation warning and returns None for None
- [ ] Implement `validate_source(source: str) -> str` -- validates against VALID_SOURCES
- [ ] Implement `build_metadata(data_class: str | None, source: str, as_of: str | None = None, expires_at: str | None = None) -> dict` -- builds the metadata dict using validate_data_class and validate_source; enforces expires_at requirement for timed-event

**Verify:** `pytest tests/unit/test_memory_schema.py -v`

---

### Step 2: Update `memory_store` and `memory_store_direct` with metadata enforcement

**Files:**
- Modify: `agents/memory.py` -- add `data_class`, `as_of`, `expires_at` parameters to both `memory_store` and `memory_store_direct`; call `build_metadata` and include fields in the Cypher CREATE

**Test First (unit):** `tests/unit/test_memory_store_metadata.py`
- [ ] `test_memory_store_direct_with_data_class_includes_metadata` -- mocks Neo4j driver and _embed; calls memory_store_direct with data_class="person"; asserts Cypher query includes tier, as_of, data_class, superseded, source fields
- [ ] `test_memory_store_direct_unknown_class_returns_prompt` -- calls with data_class="unknown-class"; asserts JSON response contains prompt text, stored=False
- [ ] `test_memory_store_direct_without_data_class_logs_deprecation` -- calls without data_class; asserts deprecation warning logged; entry still stored (backward compat)
- [ ] `test_memory_store_direct_timed_event_without_expires_returns_error` -- calls with data_class="timed-event", no expires_at; asserts error response
- [ ] `test_memory_store_direct_timed_event_with_expires_includes_field` -- calls with data_class="timed-event", expires_at="2026-04-01T00:00:00Z"; asserts expires_at in Cypher params
- [ ] `test_memory_store_direct_invalid_source_returns_error` -- calls with source="random"; asserts error response
- [ ] `test_memory_store_with_hitl_passes_data_class_through` -- mocks HITL gate to approve; asserts data_class flows to memory_store_direct
- [ ] `test_memory_store_return_includes_data_class_in_response` -- asserts JSON response includes data_class field

**Then Implement:**
- [ ] Add `from core.memory_schema import validate_data_class, build_metadata, validate_source` to `agents/memory.py`
- [ ] Update `memory_store_direct` signature: add `data_class: str | None = None`, `as_of: str | None = None`, `expires_at: str | None = None`
- [ ] At top of `memory_store_direct`, call `validate_data_class(data_class)` -- catch ValueError, return JSON error with prompt
- [ ] Call `build_metadata(data_class, source, as_of, expires_at)` and merge metadata fields into the Cypher CREATE node properties
- [ ] Update `memory_store` signature to match, passing new params through to `memory_store_direct`
- [ ] Include `data_class` in the JSON return value

**Verify:** `pytest tests/unit/test_memory_store_metadata.py -v`

---

### Step 3: Update `graph_upsert` and `graph_upsert_direct` with metadata enforcement

**Files:**
- Modify: `agents/knowledge_graph.py` -- add `data_class`, `as_of`, `source` parameters to both `graph_upsert` and `graph_upsert_direct`; add metadata fields to MERGE SET clause on both nodes and edges

**Test First (unit):** `tests/unit/test_graph_upsert_metadata.py`
- [ ] `test_graph_upsert_direct_with_data_class_sets_metadata_on_node` -- mocks Neo4j driver; calls with data_class="person"; asserts metadata props (tier, as_of, data_class, source, superseded) are in the SET clause params
- [ ] `test_graph_upsert_direct_unknown_class_returns_prompt` -- calls with data_class="unknown-class"; asserts JSON error with prompt
- [ ] `test_graph_upsert_direct_without_data_class_logs_deprecation` -- calls without data_class; asserts deprecation warning logged; node still upserted
- [ ] `test_graph_upsert_direct_metadata_on_relationship_target` -- calls with relation and target; asserts target node also gets metadata
- [ ] `test_graph_upsert_direct_invalid_source_returns_error` -- calls with source="random"; asserts error
- [ ] `test_graph_upsert_with_hitl_passes_data_class_through` -- mocks HITL gate; asserts data_class param flows through
- [ ] `test_graph_upsert_return_includes_data_class` -- asserts JSON response includes data_class

**Then Implement:**
- [ ] Add `from core.memory_schema import validate_data_class, build_metadata, validate_source` to `agents/knowledge_graph.py`
- [ ] Update `graph_upsert_direct` signature: add `data_class: str | None = None`, `as_of: str | None = None`, `source: str = "user"`
- [ ] At top of `graph_upsert_direct`, call `validate_data_class(data_class)` -- catch ValueError, return JSON error
- [ ] Call `build_metadata(data_class, source, as_of)` and merge into the `SET n +=` properties dict
- [ ] When a relationship target is created/merged, also set metadata on the target node
- [ ] Set metadata properties on relationship itself (as_of, source, data_class)
- [ ] Update `graph_upsert` signature to match, passing new params through
- [ ] Include `data_class` in the JSON return value
- [ ] Follow existing `_validate_label` and `_validate_relation` pattern from `agents/knowledge_graph.py`

**Verify:** `pytest tests/unit/test_graph_upsert_metadata.py -v`

---

### Step 4: Create Neo4j index migration for metadata fields

**Files:**
- Modify: `agents/memory.py` -- extend `_ensure_index` to also create property indexes for `tier`, `data_class`, `expires_at`, `source`
- Modify: `agents/knowledge_graph.py` -- add `_ensure_metadata_indexes` that creates the same property indexes on entity nodes

**Test First (unit):** `tests/unit/test_memory_neo4j_indexes.py`
- [ ] `test_ensure_index_creates_vector_index` -- mocks Neo4j session; asserts the existing vector index CREATE is still called
- [ ] `test_ensure_index_creates_tier_index` -- asserts Cypher for `CREATE INDEX ... FOR (m:Memory) ON (m.tier)` is executed
- [ ] `test_ensure_index_creates_data_class_index` -- asserts Cypher for data_class index is executed
- [ ] `test_ensure_index_creates_expires_at_index` -- asserts Cypher for expires_at index is executed
- [ ] `test_ensure_index_creates_source_index` -- asserts Cypher for source index is executed
- [ ] `test_ensure_index_idempotent` -- calls _ensure_index twice; asserts CREATE INDEX only runs on first call (same global guard pattern)

**Then Implement:**
- [ ] In `agents/memory.py`, extend `_ensure_index(session)` to run additional `CREATE INDEX ... IF NOT EXISTS` statements for each metadata field on `:Memory` nodes
- [ ] In `agents/knowledge_graph.py`, add a `_ensure_metadata_indexes(session)` function that creates property indexes on all entity types (`Person`, `Project`, `System`, `Concept`, `Preference`) for `tier`, `data_class`, `source` -- use the same global `_index_created` guard pattern from `agents/memory.py`
- [ ] Call `_ensure_metadata_indexes` from `graph_upsert_direct` alongside the existing driver session

**Verify:** `pytest tests/unit/test_memory_neo4j_indexes.py -v`

---

### Step 5: Backward compatibility and deprecation warning tests

**Files:**
- No new files; tests exercise behavior established in Steps 1-3

**Test First (unit):** `tests/unit/test_memory_backward_compat.py`
- [ ] `test_memory_store_direct_no_data_class_still_stores` -- calls memory_store_direct without data_class; asserts stored=True (backward compat)
- [ ] `test_graph_upsert_direct_no_data_class_still_upserts` -- calls graph_upsert_direct without data_class; asserts upserted=True
- [ ] `test_memory_store_direct_no_data_class_logs_deprecation_warning` -- captures log output; asserts "deprecation" in logged message
- [ ] `test_graph_upsert_direct_no_data_class_logs_deprecation_warning` -- captures log output; asserts "deprecation" in logged message
- [ ] `test_memory_retrieve_returns_entries_with_and_without_metadata` -- mocks Neo4j to return mixed entries (some with data_class, some without); asserts all are returned

**Test First (integration):** `tests/integration/test_memory_metadata_flow.py`
- [ ] `test_store_then_retrieve_preserves_metadata` -- mocks Neo4j and embedding; stores with data_class="preference"; retrieves and asserts metadata fields present in returned data
- [ ] `test_epilogue_write_to_memory_without_data_class_still_works` -- patches memory_store_direct and graph_upsert_direct; calls write_to_memory with a digest; asserts calls succeed (no crash from missing data_class during transition period)

**Then Implement:**
- [ ] No new implementation code -- these tests validate the behavior already built in Steps 1-4
- [ ] If any test fails, fix the implementation to match the backward-compat requirement

**Verify:** `pytest tests/unit/test_memory_backward_compat.py tests/integration/test_memory_metadata_flow.py -v`

---

### Step 6: Update `memory_retrieve` to surface metadata fields

**Files:**
- Modify: `agents/memory.py` -- update `memory_retrieve` Cypher query RETURN clause to include `data_class`, `tier`, `as_of`, `expires_at`, `superseded`, and surface them in the response JSON

**Test First (unit):** `tests/unit/test_memory_retrieve_metadata.py`
- [ ] `test_memory_retrieve_includes_data_class_in_results` -- mocks Neo4j to return records with data_class field; asserts JSON response includes data_class per memory
- [ ] `test_memory_retrieve_includes_tier_in_results` -- asserts tier is present
- [ ] `test_memory_retrieve_includes_as_of_in_results` -- asserts as_of is present
- [ ] `test_memory_retrieve_handles_entries_without_metadata` -- mocks Neo4j to return records missing data_class (pre-migration); asserts no crash, field defaults to None

**Then Implement:**
- [ ] Add `m.data_class AS data_class, m.tier AS tier, m.as_of AS as_of, m.expires_at AS expires_at, m.superseded AS superseded` to both Cypher RETURN clauses in `memory_retrieve`
- [ ] Include these fields in the dict comprehension that builds the response, using `.get()` with None default for backward compat

**Verify:** `pytest tests/unit/test_memory_retrieve_metadata.py -v`

---

## Integration Checklist

- [ ] No route changes in `server.py` (this story is purely agent/core layer)
- [ ] MCP tools (`memory_store`, `graph_upsert`) gain new optional params; auto-discovered, no registration changes needed in `mcp_server.py`
- [ ] No config additions in `config.py` / `config.yaml` (registry is code-level, not config)
- [ ] No new dependencies in `requirements.txt` (uses only stdlib `dataclasses`, `datetime`, `warnings`, `logging`)
- [ ] No secrets changes

## Build Verification

- [ ] `pytest -v` passes (all existing + new tests)
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `ruff check .` passes
- [ ] All ACs addressed:
  - AC1: `memory_store` accepts and validates `data_class` (Step 2)
  - AC2: Unknown `data_class` triggers prompt (Steps 1, 2, 3)
  - AC3: All new entries include full metadata (Steps 2, 3)
  - AC4: `graph_upsert` enforces metadata (Step 3)
  - AC5: Neo4j indexes created (Step 4)
  - AC6: Class registry constant with 7 classes (Step 1)
  - AC7: Existing entries unchanged (Step 5)
  - AC8: `data_class` optional with deprecation warning (Steps 1, 5)
  - AC9: All tests pass (Build Verification)
  - AC10: Code review approved (separate step)
