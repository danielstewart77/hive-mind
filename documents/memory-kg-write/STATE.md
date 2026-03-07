# Story State Tracker

Story: [Memory] Knowledge Graph Write Procedure — Disambiguation & Orphan Guard
Card: 1723685886631085593
Branch: story/memory-kg-write

## Progress

- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][X] Implement with TDD (remediation applied)
- [state 4][X] Code review
- [state 5][ ] Ready for merge

## Acceptance Criteria

- [X] Query-first check in `graph_upsert` — before creating any node, call `graph_query` for the proposed entity name
  - [X] If existing node with same or similar name is found:
    - [X] Clearly identical → merge/update, do not create a new node
    - [X] Possibly the same (similar name, same domain) → do not write; send disambiguation message
    - [X] Wait for confirmation before proceeding
  - [X] If no similar node found → proceed with write
- [X] Orphan node guard in `graph_upsert` — reject writes where no `relation` and `target_name` are provided
  - [X] Return clear error message: "Cannot create a node without at least one edge. Provide a relation and target, or defer until the relationship is known."
  - [X] Grace period exception: nodes created within an active session window (30 minutes) may temporarily exist without edges
- [X] Disambiguation message format
  - [X] Send via Telegram (not full HITL — no blocking approval flow)
  - [X] Include: proposed node name, matching existing node(s), simple yes/no/new choice
- [X] Orphan cleanup (Pass 5)
  - [X] Nightly job: find all graph nodes with zero edges older than grace period
  - [X] Log and send to Daniel for review (batch, not one per message)
  - [X] Do not auto-delete — always surface for human review
- [X] Tests
  - [X] Duplicate node is caught and disambiguation message sent
  - [X] Orphan node write is rejected
  - [X] Grace period allows temporary orphans in active sessions
  - [X] Nightly pass identifies stale orphans
