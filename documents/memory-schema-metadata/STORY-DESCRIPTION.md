# [Memory] Schema & Metadata — Foundation

**Card ID:** 1723685509068228112

## Description

Foundation story. All other memory lifecycle stories depend on this.

Update `memory_store` and `graph_upsert` to enforce the data classification model defined in `specs/memory-lifecycle.md`.

## Requirements

### 1. `memory_store` updates
- Add required `data_class` parameter (e.g. `technical-config`, `session-log`, `timed-event`, `person`, `world-event`, `preference`, `intention`)
- If `data_class` is not in the known class registry → do not store; return a prompt for Daniel: "I don't have a class defined for [description]. Should I define one, discard it, or handle it differently?"
- Add metadata fields to every stored entry:
  - `tier`: derived from `data_class` (`reviewable` or `durable`)
  - `as_of`: ISO date (auto-set to now if not provided)
  - `expires_at`: required for `timed-event` class only; omit otherwise
  - `source`: `user`, `tool`, `session`, or `self`
  - `superseded`: boolean, default `False`
  - `data_class`: the class name

### 2. `graph_upsert` updates
- Same `data_class`, `tier`, `as_of`, `source` fields on every node and edge
- `superseded` flag for durable nodes that have been updated

### 3. Neo4j schema
- Ensure new fields are indexed appropriately for pruning queries
- Existing entries without `data_class` are left as-is (handled by backfill story)

### 4. Class registry
- Define the known class list as a constant in `agents/memory.py`
- Easy to extend when new classes are added

## Acceptance Criteria

- [ ] `memory_store` accepts and validates `data_class` parameter against registry
- [ ] Unknown `data_class` values trigger a prompt to Daniel; memory is not stored
- [ ] All new `memory_store` entries include: `tier`, `as_of`, `expires_at` (timed-event only), `source`, `superseded`, `data_class`
- [ ] `graph_upsert` enforces same metadata fields on all nodes and edges
- [ ] Neo4j indexes created for: `tier`, `data_class`, `expires_at`, `source`
- [ ] Class registry constant defined in `agents/memory.py` with all 7 classes: `technical-config`, `session-log`, `timed-event`, `person`, `world-event`, `preference`, `intention`
- [ ] Existing entries without `data_class` remain unchanged (backward compatible)
- [ ] `data_class` is optional with deprecation warning until backfill is complete, then becomes required
- [ ] All tests pass (new + regression)
- [ ] Code review approved

## Tasks

- [ ] Read and understand `specs/memory-lifecycle.md` data classification model
- [ ] Design data class registry structure in `agents/memory.py`
- [ ] Update `memory_store` signature and implementation with validation logic
- [ ] Update `graph_upsert` signature and implementation with metadata enforcement
- [ ] Create Neo4j migration script for index creation
- [ ] Write comprehensive test suite for class validation
- [ ] Write comprehensive test suite for metadata enforcement
- [ ] Write test suite for unknown class prompting
- [ ] Test backward compatibility with existing entries
- [ ] Integration test: store → retrieve → verify metadata intact
- [ ] Code review
- [ ] Prepare commit and PR
