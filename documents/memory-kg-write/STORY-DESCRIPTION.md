# [Memory] Knowledge Graph Write Procedure — Disambiguation & Orphan Guard

**Card ID:** 1723685886631085593

## Description

Enforce the knowledge graph write procedure from `specs/memory-lifecycle.md`. Two key guards: disambiguate before writing (no duplicate/fragmented nodes), and reject orphan nodes (no edges = no write).

Depends on: Schema & Metadata story.

## Acceptance Criteria

- [x] Query-first check in `graph_upsert` — before creating any node, call `graph_query` for the proposed entity name
  - [x] If existing node with same or similar name is found:
    - [x] Clearly identical → merge/update, do not create a new node
    - [x] Possibly the same (similar name, same domain) → do not write; send disambiguation message
    - [x] Wait for confirmation before proceeding
  - [x] If no similar node found → proceed with write
- [x] Orphan node guard in `graph_upsert` — reject writes where no `relation` and `target_name` are provided
  - [x] Return clear error message: "Cannot create a node without at least one edge. Provide a relation and target, or defer until the relationship is known."
  - [x] Grace period exception: nodes created within an active session window (30 minutes suggested) may temporarily exist without edges
- [x] Disambiguation message format
  - [x] Send via Telegram (not full HITL — no blocking approval flow)
  - [x] Include: proposed node name, matching existing node(s), simple yes/no/new choice
- [x] Orphan cleanup (Pass 5)
  - [x] Nightly job: find all graph nodes with zero edges older than grace period
  - [x] Log and send to Daniel for review (batch, not one per message)
  - [x] Do not auto-delete — always surface for human review
- [x] Tests
  - [x] Duplicate node is caught and disambiguation message sent
  - [x] Orphan node write is rejected
  - [x] Grace period allows temporary orphans in active sessions
  - [x] Nightly pass identifies stale orphans

## Tasks

- Implement query-first check in `graph_upsert` function
- Implement orphan node guard with error messaging
- Add disambiguation prompt via Telegram notification
- Implement nightly orphan cleanup pass
- Add comprehensive test suite covering all scenarios
- Update `agents/memory.py` with new write procedure guards
