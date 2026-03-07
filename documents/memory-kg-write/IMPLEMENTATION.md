# Implementation Plan: 1723685886631085593 - Knowledge Graph Write Procedure: Disambiguation & Orphan Guard

## Overview

Enforce the knowledge graph write procedure from `specs/memory-lifecycle.md` by adding two guards to `graph_upsert`: (1) a query-first disambiguation check that detects duplicate/similar nodes before writing, and (2) an orphan node guard that rejects writes without at least one edge (with a grace period for active sessions). A nightly orphan cleanup sweep finds stale orphan nodes and surfaces them to Daniel for review via Telegram.

## Technical Approach

The implementation adds a new core module `core/kg_guards.py` containing the disambiguation and orphan guard logic, keeping it testable independently of Neo4j and Telegram. The `graph_upsert` tool in `agents/knowledge_graph.py` is modified to call these guards before proceeding with writes. The `graph_upsert_direct` function (used by the epilogue) also gets the orphan guard but skips disambiguation since the epilogue already handles batch approval. A new `core/orphan_sweep.py` module handles the nightly cleanup pass. The scheduler gets a new job entry for the nightly orphan sweep, and `server.py` gets a new endpoint (`/memory/orphan-sweep`) following the existing `/memory/expiry-sweep` pattern.

Key design decisions:
- **Disambiguation uses a Cypher `CONTAINS` query** (case-insensitive) to find similar nodes, rather than full fuzzy matching. This keeps the implementation simple and avoids new dependencies. The query searches for nodes where the name contains the proposed name or vice versa.
- **Orphan guard checks `relation` and `target_name` parameters** at the function level, not in Cypher. If both are empty, the write is rejected unless a grace period exception applies.
- **Grace period is tracked via a `created_at` timestamp on nodes** (epoch float). The orphan guard accepts writes without edges during the grace window (30 minutes). The orphan sweep checks this timestamp to identify stale orphans.
- **Disambiguation messages use Telegram direct send** (non-blocking, no HITL), following the pattern in `agents/notify.py::_telegram_direct`.
- **The orphan sweep returns a result dict** following the `sweep_expired_events` pattern in `core/memory_expiry.py`.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|------------|-------|
| Memory expiry sweep module | `core/memory_expiry.py` | Pattern for orphan sweep: lazy driver import, Telegram notification, result dict |
| Memory expiry sweep tests | `tests/unit/test_memory_expiry_sweep.py` | Pattern for testing sweep functions with mock Neo4j driver |
| Memory expiry API endpoint | `tests/api/test_memory_expiry_sweep.py` | Pattern for testing authenticated sweep endpoints |
| Memory expiry scheduler job | `tests/unit/test_memory_expiry_scheduler.py` | Pattern for testing scheduler integration |
| Graph upsert metadata tests | `tests/unit/test_graph_upsert_metadata.py` | Pattern for testing graph_upsert with mock driver |
| Notify module Telegram direct | `agents/notify.py::_telegram_direct` | Pattern for non-blocking Telegram messages |
| Knowledge graph module | `agents/knowledge_graph.py` | Existing graph_upsert, graph_query, HITL gate |

## Models & Schemas

### DisambiguationResult (in `core/kg_guards.py`)

```python
@dataclass
class DisambiguationResult:
    action: str          # "proceed", "merge", "disambiguate"
    existing_nodes: list[dict]  # matching nodes from graph_query
    message: str         # human-readable explanation
```

No new Pydantic models needed for the API -- the `/memory/orphan-sweep` endpoint reuses the same dict-return pattern as `/memory/expiry-sweep`.

## Implementation Steps

### Step 1: Disambiguation logic in `core/kg_guards.py`

Create the core disambiguation function that queries the knowledge graph for similar nodes and returns a decision.

**Files:**
- Create: `core/kg_guards.py` -- disambiguation and orphan guard logic
- Create: `tests/unit/test_kg_guards.py` -- unit tests for guard logic

**Test First (unit):** `tests/unit/test_kg_guards.py`
- [ ] `test_check_disambiguation_no_existing_returns_proceed` -- asserts action="proceed" when graph_query returns found=False
- [ ] `test_check_disambiguation_exact_match_returns_merge` -- asserts action="merge" when graph_query returns a node with identical name
- [ ] `test_check_disambiguation_similar_name_returns_disambiguate` -- asserts action="disambiguate" when fuzzy query returns a similar but not identical node
- [ ] `test_check_disambiguation_different_entity_type_still_checks` -- asserts disambiguation works across entity types
- [ ] `test_check_disambiguation_case_insensitive_match` -- asserts "daniel" matches "Daniel"
- [ ] `test_check_disambiguation_returns_existing_nodes` -- asserts existing_nodes list is populated with matching node data
- [ ] `test_check_disambiguation_message_includes_node_names` -- asserts the message string contains both proposed and existing node names

**Then Implement:**
- [ ] Create `core/kg_guards.py` with `check_disambiguation(name: str, entity_type: str, agent_id: str) -> DisambiguationResult`
- [ ] The function runs a Cypher query: `MATCH (n) WHERE n.agent_id = $agent_id AND (toLower(n.name) = toLower($name) OR toLower(n.name) CONTAINS toLower($name) OR toLower($name) CONTAINS toLower(n.name)) RETURN n.name AS name, labels(n) AS labels, elementId(n) AS id`
- [ ] Decision logic: exact match (case-insensitive) = "merge"; similar match (contains) = "disambiguate"; no match = "proceed"
- [ ] Import `_get_driver` from `agents.knowledge_graph` lazily (same pattern as `core/memory_expiry.py`)

**Verify:** `pytest tests/unit/test_kg_guards.py -v`

---

### Step 2: Orphan node guard in `core/kg_guards.py`

Add the orphan guard function that rejects writes without edges unless within the grace period.

**Files:**
- Modify: `core/kg_guards.py` -- add `check_orphan_guard` function
- Modify: `tests/unit/test_kg_guards.py` -- add orphan guard tests

**Test First (unit):** `tests/unit/test_kg_guards.py`
- [ ] `test_orphan_guard_rejects_no_relation_no_target` -- asserts rejection when relation="" and target_name=""
- [ ] `test_orphan_guard_allows_with_relation_and_target` -- asserts allowed=True when both are provided
- [ ] `test_orphan_guard_rejects_relation_without_target` -- asserts rejection when relation is set but target_name is empty
- [ ] `test_orphan_guard_error_message_matches_spec` -- asserts the error message contains "Cannot create a node without at least one edge"
- [ ] `test_orphan_guard_grace_period_allows_orphan` -- asserts allowed=True when grace_period=True is passed
- [ ] `test_orphan_guard_grace_period_default_false` -- asserts default behavior rejects orphans

**Then Implement:**
- [ ] Add `check_orphan_guard(relation: str, target_name: str, grace_period: bool = False) -> tuple[bool, str]` to `core/kg_guards.py`
- [ ] Returns `(True, "")` if relation and target_name are both non-empty, or if grace_period=True
- [ ] Returns `(False, "Cannot create a node without at least one edge. Provide a relation and target, or defer until the relationship is known.")` otherwise

**Verify:** `pytest tests/unit/test_kg_guards.py -v`

---

### Step 3: Telegram disambiguation notification

Add the function to send disambiguation messages via Telegram (non-blocking, not HITL).

**Files:**
- Modify: `core/kg_guards.py` -- add `send_disambiguation_message` function
- Modify: `tests/unit/test_kg_guards.py` -- add Telegram notification tests

**Test First (unit):** `tests/unit/test_kg_guards.py`
- [ ] `test_send_disambiguation_message_calls_telegram` -- asserts `_telegram_direct` is called with the formatted message
- [ ] `test_send_disambiguation_message_includes_proposed_name` -- asserts message contains the proposed node name
- [ ] `test_send_disambiguation_message_includes_existing_nodes` -- asserts message lists existing matching node names
- [ ] `test_send_disambiguation_message_includes_choice_options` -- asserts message contains "yes/no/new" or equivalent choice text
- [ ] `test_send_disambiguation_message_handles_telegram_failure` -- asserts graceful handling when Telegram fails (returns False, does not raise)

**Then Implement:**
- [ ] Add `send_disambiguation_message(proposed_name: str, existing_nodes: list[dict]) -> bool` to `core/kg_guards.py`
- [ ] Format message: "I'm about to add [{proposed_name}] to the graph. Found similar nodes:\n{list of existing names}\n\nIs this the same entity? (yes = merge, no = create new, skip = defer)"
- [ ] Use lazy import of `agents.notify._telegram_direct` (same pattern as `core/memory_expiry.py::_telegram_direct`)
- [ ] Return True if Telegram send succeeds, False otherwise

**Verify:** `pytest tests/unit/test_kg_guards.py -v`

---

### Step 4: Integrate guards into `graph_upsert` (HITL-gated path)

Modify the `graph_upsert` tool function to call disambiguation and orphan guard before writing.

**Files:**
- Modify: `agents/knowledge_graph.py` -- add guard calls to `graph_upsert`
- Create: `tests/unit/test_kg_write_guards.py` -- integration tests for guards in graph_upsert

**Test First (unit):** `tests/unit/test_kg_write_guards.py`
- [ ] `test_graph_upsert_calls_disambiguation_before_write` -- asserts check_disambiguation is called before _hitl_gate
- [ ] `test_graph_upsert_exact_match_proceeds_to_merge` -- asserts that when disambiguation returns "merge", the existing MERGE Cypher still runs (it already does MERGE, so identical names update in place)
- [ ] `test_graph_upsert_similar_name_sends_disambiguation_and_rejects` -- asserts write is rejected and disambiguation message sent when similar node found
- [ ] `test_graph_upsert_no_match_proceeds_normally` -- asserts write proceeds when no similar node found
- [ ] `test_graph_upsert_orphan_guard_rejects_no_edges` -- asserts write is rejected with correct error message when no relation/target
- [ ] `test_graph_upsert_orphan_guard_allows_with_edges` -- asserts write proceeds when relation and target_name provided
- [ ] `test_graph_upsert_disambiguation_result_in_response` -- asserts the returned JSON includes disambiguation info when rejected
- [ ] `test_graph_upsert_orphan_rejection_returns_json_error` -- asserts returned JSON contains the orphan error message

**Then Implement:**
- [ ] In `graph_upsert()`, before HITL gate, call `check_orphan_guard(relation, target_name)`. If rejected, return JSON error immediately
- [ ] After orphan guard passes, call `check_disambiguation(name, entity_type, agent_id)` from `core.kg_guards`
- [ ] If result.action == "disambiguate": call `send_disambiguation_message()`, return JSON with `{"upserted": False, "reason": "disambiguation_required", "similar_nodes": [...]}`
- [ ] If result.action == "merge" or "proceed": continue to HITL gate and write
- [ ] Follow existing error handling pattern: wrap in try/except, return JSON errors

**Verify:** `pytest tests/unit/test_kg_write_guards.py -v`

---

### Step 5: Integrate orphan guard into `graph_upsert_direct` (epilogue path)

The epilogue calls `graph_upsert_direct` which bypasses HITL. Add the orphan guard here too, but with a grace period since the epilogue writes entities first and relationships separately.

**Files:**
- Modify: `agents/knowledge_graph.py` -- add orphan guard to `graph_upsert_direct` with grace_period=True
- Modify: `tests/unit/test_kg_write_guards.py` -- add tests for direct path

**Test First (unit):** `tests/unit/test_kg_write_guards.py`
- [ ] `test_graph_upsert_direct_with_relation_proceeds` -- asserts write succeeds with relation and target
- [ ] `test_graph_upsert_direct_without_relation_uses_grace_period` -- asserts that `graph_upsert_direct` passes through (grace period enabled for epilogue use case)
- [ ] `test_graph_upsert_direct_adds_created_at_to_node` -- asserts that a `created_at` epoch timestamp is added to node properties for orphan sweep tracking

**Then Implement:**
- [ ] In `graph_upsert_direct()`, add `import time` and set `props["created_at"] = time.time()` before writing
- [ ] Note: We do NOT apply the orphan guard to `graph_upsert_direct` since it is the epilogue path and entities are written before relationships. The epilogue flow handles this by writing relationships in a second pass. The `created_at` timestamp enables the orphan sweep to identify stale orphans.

**Verify:** `pytest tests/unit/test_kg_write_guards.py -v`

---

### Step 6: Orphan sweep module `core/orphan_sweep.py`

Create the nightly orphan cleanup sweep following the `core/memory_expiry.py` pattern.

**Files:**
- Create: `core/orphan_sweep.py` -- orphan node sweep logic
- Create: `tests/unit/test_orphan_sweep.py` -- unit tests for sweep

**Test First (unit):** `tests/unit/test_orphan_sweep.py`
- [ ] `test_sweep_finds_orphan_nodes_without_edges` -- asserts nodes with zero edges are returned
- [ ] `test_sweep_respects_grace_period` -- asserts nodes created within 30 minutes are not flagged
- [ ] `test_sweep_flags_stale_orphans_past_grace_period` -- asserts nodes older than 30 minutes with zero edges are flagged
- [ ] `test_sweep_sends_batch_telegram_message` -- asserts a single batched Telegram message is sent listing all stale orphans
- [ ] `test_sweep_does_not_auto_delete` -- asserts no DELETE Cypher is executed
- [ ] `test_sweep_empty_results_no_telegram` -- asserts no Telegram message when no orphans found
- [ ] `test_sweep_telegram_failure_does_not_raise` -- asserts graceful handling of Telegram errors
- [ ] `test_sweep_returns_result_dict` -- asserts return dict has keys: orphans_found, notified, errors
- [ ] `test_sweep_logs_orphan_details` -- asserts orphan node names are logged

**Then Implement:**
- [ ] Create `core/orphan_sweep.py` with `sweep_orphan_nodes() -> dict`
- [ ] Use lazy import pattern: `_get_driver()` from `agents.knowledge_graph`, `_telegram_direct()` from `agents.notify`
- [ ] Cypher query: find all entity nodes (labels in `_VALID_ENTITY_TYPES`) that have zero relationships and `created_at < (now - 1800)` (30 minute grace period). Query: `MATCH (n) WHERE n.agent_id = $agent_id AND NOT (n)--() AND n.created_at < $cutoff RETURN n.name AS name, labels(n) AS labels, n.created_at AS created_at, elementId(n) AS id`
- [ ] Format batch Telegram message: "Orphan nodes found (no edges, older than 30 min):\n{list}\n\nReview and connect or remove manually."
- [ ] Do NOT delete any nodes -- only log and notify
- [ ] Return `{"orphans_found": N, "notified": bool, "errors": int}`

**Verify:** `pytest tests/unit/test_orphan_sweep.py -v`

---

### Step 7: Server endpoint for orphan sweep

Add the `/memory/orphan-sweep` endpoint to `server.py`, following the existing `/memory/expiry-sweep` pattern.

**Files:**
- Modify: `server.py` -- add `/memory/orphan-sweep` endpoint
- Create: `tests/api/test_orphan_sweep.py` -- API tests for the endpoint

**Test First (API):** `tests/api/test_orphan_sweep.py`
- [ ] `test_orphan_sweep_endpoint_returns_200` -- asserts 200 with valid auth
- [ ] `test_orphan_sweep_rejects_missing_token` -- asserts 401 without auth header
- [ ] `test_orphan_sweep_rejects_wrong_token` -- asserts 401 with wrong auth header
- [ ] `test_orphan_sweep_returns_result_counts` -- asserts response body includes orphans_found, notified, errors

**Then Implement:**
- [ ] Add endpoint following exact pattern of `memory_expiry_sweep()` in `server.py`
- [ ] Import `sweep_orphan_nodes` from `core.orphan_sweep` (lazy, inside function body)
- [ ] Run via `asyncio.to_thread(sweep_orphan_nodes)` to avoid blocking

**Verify:** `pytest tests/api/test_orphan_sweep.py -v`

---

### Step 8: Scheduler integration for nightly orphan sweep

Add the orphan sweep job to the scheduler, following the existing memory expiry sweep pattern.

**Files:**
- Modify: `clients/scheduler.py` -- add `_orphan_sweep` function and scheduler job
- Create: `tests/unit/test_orphan_sweep_scheduler.py` -- scheduler integration tests

**Test First (unit):** `tests/unit/test_orphan_sweep_scheduler.py`
- [ ] `test_orphan_sweep_calls_endpoint` -- asserts POST to `/memory/orphan-sweep` with correct auth header
- [ ] `test_orphan_sweep_logs_results` -- asserts sweep results are logged
- [ ] `test_orphan_sweep_handles_failure` -- asserts graceful handling when endpoint is unreachable

**Then Implement:**
- [ ] Add `_orphan_sweep()` async function in `clients/scheduler.py` following `_memory_expiry_sweep()` pattern exactly
- [ ] Add scheduler job in `main()`: `CronTrigger(hour="3", minute="45", timezone="America/Chicago")` -- runs nightly at 3:45 AM CT (15 min after memory expiry sweep)
- [ ] Log: "Scheduled orphan sweep @ 45 3 * * *"

**Verify:** `pytest tests/unit/test_orphan_sweep_scheduler.py -v`

---

### Step 9: Integration tests for the full disambiguation and orphan flow

End-to-end tests verifying the complete flow through graph_upsert with guards.

**Files:**
- Create: `tests/integration/test_kg_write_guards_flow.py` -- integration tests

**Test First (integration):** `tests/integration/test_kg_write_guards_flow.py`
- [ ] `test_disambiguation_blocks_write_and_sends_telegram` -- full flow: graph_upsert called with a name that has a similar existing node; asserts write is rejected and Telegram message sent
- [ ] `test_orphan_guard_blocks_graph_upsert_without_edges` -- full flow: graph_upsert called without relation/target; asserts rejection with correct error message
- [ ] `test_grace_period_allows_temporary_orphan_via_direct` -- graph_upsert_direct without relation succeeds (for epilogue use), and created_at timestamp is set on the node
- [ ] `test_orphan_sweep_finds_stale_orphan_and_notifies` -- sweep_orphan_nodes finds a node older than grace period with no edges; asserts notification sent

**Then Implement:**
- [ ] These tests use the same mock Neo4j driver pattern from `tests/integration/test_memory_metadata_flow.py`
- [ ] Each test exercises the full call chain: `graph_upsert` -> `core.kg_guards` -> Neo4j mock -> return value assertions
- [ ] Mock `_telegram_direct` to capture notification messages

**Verify:** `pytest tests/integration/test_kg_write_guards_flow.py -v`

---

## Integration Checklist

- [ ] Routes registered in `server.py` -- `/memory/orphan-sweep` endpoint added
- [N/A] MCP tools decorated and discoverable in `agents/` -- no new tools, existing `graph_upsert` modified
- [N/A] Config additions in `config.py` / `config.yaml` -- no new config needed
- [N/A] Dependencies added to `requirements.txt` -- no new dependencies
- [N/A] Secrets stored in keyring -- no new secrets

## Build Verification

- [ ] `pytest -v` passes
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `ruff check .` passes
- [ ] All ACs addressed:
  - [ ] AC: Query-first check in graph_upsert (Steps 1, 4)
  - [ ] AC: Identical node merge (Steps 1, 4)
  - [ ] AC: Similar name disambiguation message (Steps 1, 3, 4)
  - [ ] AC: Orphan node guard rejection (Steps 2, 4)
  - [ ] AC: Grace period exception (Steps 2, 5)
  - [ ] AC: Disambiguation via Telegram (Step 3)
  - [ ] AC: Nightly orphan cleanup (Steps 6, 7, 8)
  - [ ] AC: Batch notification, no auto-delete (Step 6)
  - [ ] AC: Test coverage for all scenarios (Steps 1-9)
