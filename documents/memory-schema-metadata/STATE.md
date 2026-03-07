# Story State Tracker

Story: [Memory] Schema & Metadata — Foundation
Card: 1723685509068228112
Branch: story/memory-schema-metadata

## Progress

- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][X] Implement with TDD
- [state 4][X] Code review
- [state 5][ ] Ready for merge

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

## Notes

This is a foundation story — all other memory lifecycle stories depend on it. The implementation enforces the data classification model defined in `specs/memory-lifecycle.md` with seven distinct data classes and two tiers (reviewable and durable).

Key design decisions:
- Class registry is a constant in `agents/memory.py` — easy to extend
- Unknown classes return a prompt to Daniel for interactive classification
- Metadata is enforced at write time; no orphan entries
- Backward compatible: existing entries without `data_class` remain unchanged; deprecation warning during optional phase
