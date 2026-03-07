# Implementation Plan: 1723685647471871507 - Existing Data Backfill

## Overview

Create a one-time backfill tool that scans all existing Neo4j Memory nodes and knowledge graph entity nodes lacking a `data_class` field, classifies them against the 7 defined data classes using content and tag heuristics, auto-assigns high-confidence matches, and queues ambiguous entries for Daniel's review via a batched Telegram flow. The backfill also makes `data_class` required (non-optional) in `memory_store` and `graph_upsert` once all entries are classified.

## Technical Approach

1. **Classification engine** (`core/backfill_classifier.py`): Pure-logic module with keyword/tag heuristics that map content and tags to the 7 data classes in `core/memory_schema.py`. Returns a `ClassificationResult` with class name and confidence score. High-confidence threshold = 0.7; below that, entries go to human review. This module has zero external dependencies (no Neo4j, no Telegram) and is fully unit-testable.

2. **Backfill scanner** (`agents/memory_backfill.py`): MCP tool that queries Neo4j for all Memory nodes and entity nodes where `data_class IS NULL`, runs each through the classifier, applies high-confidence assignments via Cypher UPDATE, and batches the rest for Telegram review. Uses the existing `_get_driver()` pattern from `agents/memory.py`.

3. **Telegram batch review**: Uses `agents/notify.py`'s `_telegram_direct()` pattern to send grouped review messages. Each batch message shows up to 10 entries with summaries and candidate classes. Daniel replies with `/classify_<entry_id> <class>` commands. The Telegram bot routes these to a new handler that updates the entry.

4. **Make data_class required**: After backfill completes, change `data_class: str | None = None` to `data_class: str` in both `memory_store`/`memory_store_direct` and `graph_upsert`/`graph_upsert_direct`. Update `validate_data_class()` to raise on None instead of warning. Fix `core/epilogue.py` `write_to_memory()` which passes no `data_class` -- it must classify each topic/entity before writing.

**Key constraint**: The epilogue currently passes `source="session_epilogue"` which is NOT in `VALID_SOURCES` -- this already fails validation. We need to either add `"session_epilogue"` to `VALID_SOURCES` or change the epilogue to use `source="session"`.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|------------|-------|
| Neo4j driver singleton + mock pattern | `agents/memory.py` lines 64-68, `tests/unit/test_memory_store_metadata.py` | Reuse `_get_driver()`, mock driver in tests |
| Data class registry and validation | `core/memory_schema.py` | Classification targets, `build_metadata()` |
| Telegram direct messaging | `agents/notify.py` `_telegram_direct()` | Send batch review messages |
| MCP tool with `@tool()` decorator | `agents/memory.py` | Register backfill as MCP tool |
| HITL approval flow | `core/hitl.py`, `server.py` `/hitl/request` | Not needed directly; we use Telegram commands instead |
| Test mocking for neo4j/agent_tooling | `tests/unit/test_memory_store_metadata.py` | Fixture pattern for all new tests |

## Models & Schemas

### `core/backfill_classifier.py` -- New

```python
@dataclass
class ClassificationResult:
    data_class: str | None     # The assigned class name, or None if unclassifiable
    confidence: float          # 0.0 to 1.0
    reason: str                # Human-readable explanation of the classification
    candidates: list[str]      # Top candidate classes if ambiguous
```

### `core/memory_schema.py` -- Modify

- `DATA_CLASS_KEYWORDS`: dict mapping each data class to keyword lists used by the classifier
- (Later step) `validate_data_class()`: remove None-acceptance path, make it raise on None

### `agents/memory_backfill.py` -- New

```python
@dataclass
class BackfillEntry:
    element_id: str            # Neo4j element ID
    content: str               # Memory content or entity name
    tags: str                  # Existing tags
    created_at: int | None     # Unix timestamp if available
    source: str                # Existing source field
    node_type: str             # "memory" or "entity"
    entity_type: str | None    # For entities: Person, Project, etc.
```

## Implementation Steps

### Step 1: Classification Engine

**Files:**
- Create: `core/backfill_classifier.py` -- Pure classification logic with keyword/tag heuristics
- Modify: `core/memory_schema.py` -- Add `DATA_CLASS_KEYWORDS` constant for classifier reference

**Test First (unit):** `tests/unit/test_backfill_classifier.py`
- [ ] `test_classify_person_by_tags` -- content with "person" or "durable" tags returns data_class="person" with confidence >= 0.7
- [ ] `test_classify_preference_by_content` -- content containing "prefers", "likes", "favorite" returns data_class="preference"
- [ ] `test_classify_technical_config_by_tags` -- tags containing "technical" or "reviewable,technical" returns data_class="technical-config"
- [ ] `test_classify_session_log_by_tags` -- tags containing "session" returns data_class="session-log"
- [ ] `test_classify_timed_event_by_content` -- content with date/time patterns returns data_class="timed-event"
- [ ] `test_classify_world_event_by_content` -- content about news/external events returns data_class="world-event"
- [ ] `test_classify_intention_by_content` -- content with "plan to", "want to", "goal" returns data_class="intention"
- [ ] `test_classify_ambiguous_returns_low_confidence` -- content that matches no strong pattern returns confidence < 0.7
- [ ] `test_classify_returns_candidates_for_ambiguous` -- ambiguous content returns multiple candidate classes
- [ ] `test_classify_entity_node_person_type` -- entity with entity_type="Person" returns data_class="person" with high confidence
- [ ] `test_classify_entity_node_preference_type` -- entity with entity_type="Preference" returns data_class="preference"
- [ ] `test_classify_entity_node_project_type` -- entity with entity_type="Project" returns data_class="technical-config"
- [ ] `test_classify_empty_content_returns_low_confidence` -- empty or whitespace content returns confidence < 0.7
- [ ] `test_classify_epilogue_tagged_entries` -- tags containing "session,epilogue" returns data_class="session-log"
- [ ] `test_classification_result_dataclass_fields` -- ClassificationResult has data_class, confidence, reason, candidates fields

**Then Implement:**
- [ ] Create `core/backfill_classifier.py` with `ClassificationResult` dataclass
- [ ] Add `DATA_CLASS_KEYWORDS` to `core/memory_schema.py` mapping each class to keyword lists (follow frozen dataclass pattern from existing `DataClassDef`)
- [ ] Implement `classify_entry(content, tags, entity_type=None) -> ClassificationResult` using tag-matching first (highest signal), then keyword heuristics on content, then entity_type fallback
- [ ] Implement `classify_entity_node(name, entity_type, properties) -> ClassificationResult` that maps entity types directly (Person->person, Preference->preference, Project->technical-config)

**Verify:** `pytest tests/unit/test_backfill_classifier.py -v`

---

### Step 2: Neo4j Scanner -- Query Unclassified Entries

**Files:**
- Create: `agents/memory_backfill.py` -- MCP tool for backfill execution
- No modifications to existing files

**Test First (unit):** `tests/unit/test_backfill_scanner.py`
- [ ] `test_scan_unclassified_memories_returns_entries` -- queries Memory nodes where data_class IS NULL, returns list of BackfillEntry
- [ ] `test_scan_unclassified_entities_returns_entries` -- queries entity nodes (Person, Project, etc.) where data_class IS NULL
- [ ] `test_scan_returns_empty_when_all_classified` -- returns empty list when no NULL data_class nodes exist
- [ ] `test_scan_memory_entry_fields_populated` -- each BackfillEntry has element_id, content, tags, created_at, source, node_type="memory"
- [ ] `test_scan_entity_entry_has_entity_type` -- entity BackfillEntry has entity_type set (e.g., "Person")
- [ ] `test_scan_handles_neo4j_connection_error` -- returns error JSON on driver failure

**Then Implement:**
- [ ] Create `agents/memory_backfill.py` with `BackfillEntry` dataclass
- [ ] Implement `_scan_unclassified_memories(driver) -> list[BackfillEntry]` using Cypher `MATCH (m:Memory) WHERE m.data_class IS NULL RETURN ...`
- [ ] Implement `_scan_unclassified_entities(driver) -> list[BackfillEntry]` using Cypher query across all entity labels (Person, Project, System, Concept, Preference) where data_class IS NULL
- [ ] Use `_get_driver()` pattern from `agents/memory.py`

**Verify:** `pytest tests/unit/test_backfill_scanner.py -v`

---

### Step 3: Auto-Assign High-Confidence Classifications

**Files:**
- Modify: `agents/memory_backfill.py` -- Add auto-assignment logic

**Test First (unit):** `tests/unit/test_backfill_assign.py`
- [ ] `test_assign_memory_updates_neo4j_node` -- high-confidence classification triggers Cypher SET with data_class, tier, as_of, source
- [ ] `test_assign_entity_updates_neo4j_node` -- entity classification updates the entity node with data_class and tier
- [ ] `test_assign_uses_created_at_for_as_of` -- when entry has created_at, as_of is set from it (converted to ISO datetime)
- [ ] `test_assign_defaults_as_of_to_now_when_no_created_at` -- when created_at is None, as_of defaults to current time
- [ ] `test_assign_preserves_existing_source` -- does not overwrite existing source field on the node
- [ ] `test_assign_skips_low_confidence` -- entries with confidence < 0.7 are not auto-assigned
- [ ] `test_assign_returns_counts` -- returns dict with counts: {assigned: N, skipped: N, errors: N}

**Then Implement:**
- [ ] Implement `_assign_classification(driver, entry: BackfillEntry, result: ClassificationResult) -> bool` that runs Cypher `MATCH ... WHERE elementId(n) = $id SET n.data_class = $dc, n.tier = $tier, n.as_of = $as_of`
- [ ] Implement `_auto_assign_batch(driver, entries: list[BackfillEntry]) -> dict` that classifies each entry and assigns high-confidence results
- [ ] Use `build_metadata()` from `core/memory_schema.py` for tier resolution

**Verify:** `pytest tests/unit/test_backfill_assign.py -v`

---

### Step 4: Telegram Batch Review Flow

**Files:**
- Modify: `agents/memory_backfill.py` -- Add batch review message formatting and sending
- Create: `core/backfill_review.py` -- Review message formatting and response parsing (pure logic, testable)

**Test First (unit):** `tests/unit/test_backfill_review.py`
- [ ] `test_format_review_batch_groups_entries` -- formats up to 10 entries per message with summaries and candidate classes
- [ ] `test_format_review_batch_truncates_content` -- long content is truncated to 200 chars in the review message
- [ ] `test_format_review_batch_shows_candidates` -- each entry shows top 3 candidate classes
- [ ] `test_format_review_batch_includes_entry_id` -- each entry has a short ID for reference in Daniel's replies
- [ ] `test_format_review_message_header` -- message starts with count of entries needing review
- [ ] `test_parse_classify_command_valid` -- `/classify_abc123 person` returns (entry_id, data_class)
- [ ] `test_parse_classify_command_invalid_class` -- `/classify_abc123 invalid-class` returns error
- [ ] `test_parse_classify_command_new_class` -- `/classify_abc123 new:my-new-class` returns (entry_id, "new:my-new-class") for new class registration
- [ ] `test_format_empty_review_batch` -- empty list returns "No entries need review" message

**Then Implement:**
- [ ] Create `core/backfill_review.py` with `format_review_batch(entries: list[tuple[BackfillEntry, ClassificationResult]]) -> list[str]` that returns list of formatted Telegram messages (max 4096 chars each, max 10 entries per message)
- [ ] Implement `parse_classify_command(text: str) -> tuple[str, str] | None` that parses `/classify_<id> <class>` commands
- [ ] Implement `_send_review_batches(entries: list)` in `agents/memory_backfill.py` using `_telegram_direct()` pattern from `agents/notify.py`

**Verify:** `pytest tests/unit/test_backfill_review.py -v`

---

### Step 5: Telegram Bot Command Handler for Classification Responses

**Files:**
- Modify: `clients/telegram_bot.py` -- Add `/classify_*` command handler
- Modify: `agents/memory_backfill.py` -- Add `apply_classification(element_id, data_class)` function

**Test First (unit):** `tests/unit/test_backfill_telegram_handler.py`
- [ ] `test_classify_command_applies_to_memory_node` -- `/classify_abc123 person` updates the Memory node's data_class
- [ ] `test_classify_command_applies_to_entity_node` -- `/classify_abc123 technical-config` updates an entity node
- [ ] `test_classify_command_with_new_class_adds_to_registry` -- `/classify_abc123 new:shopping-list` adds "shopping-list" to DATA_CLASS_REGISTRY and applies it
- [ ] `test_classify_command_sends_confirmation` -- bot replies with "Classified [summary] as [class]" confirmation
- [ ] `test_classify_command_invalid_id_sends_error` -- unknown entry ID returns error message
- [ ] `test_classify_command_unauthorized_user_rejected` -- non-allowed user cannot classify

**Then Implement:**
- [ ] Add `apply_classification(element_id: str, data_class: str, source: str = "user") -> str` to `agents/memory_backfill.py` that updates the node in Neo4j
- [ ] Add `register_new_class(class_name: str, tier: str = "reviewable") -> DataClassDef` to `core/memory_schema.py` that adds to `DATA_CLASS_REGISTRY` at runtime
- [ ] Add command handler in `clients/telegram_bot.py` that matches `/classify_*` pattern, calls `parse_classify_command()`, then `apply_classification()`
- [ ] Follow existing Telegram command handler pattern from `clients/telegram_bot.py` (HITL approve/deny handlers)

**Verify:** `pytest tests/unit/test_backfill_telegram_handler.py -v`

---

### Step 6: Backfill MCP Tool -- Orchestrator

**Files:**
- Modify: `agents/memory_backfill.py` -- Add `@tool` decorated `memory_backfill()` function that orchestrates the full flow

**Test First (unit):** `tests/unit/test_backfill_tool.py`
- [ ] `test_backfill_tool_returns_summary_json` -- returns JSON with total_scanned, auto_assigned, needs_review, errors counts
- [ ] `test_backfill_tool_scans_both_memory_and_entities` -- calls both memory and entity scan functions
- [ ] `test_backfill_tool_auto_assigns_high_confidence` -- high-confidence entries are assigned without review
- [ ] `test_backfill_tool_sends_review_for_low_confidence` -- low-confidence entries are sent to Telegram for review
- [ ] `test_backfill_tool_handles_no_unclassified` -- returns "all entries already classified" when no NULL data_class found
- [ ] `test_backfill_tool_handles_neo4j_unavailable` -- returns error JSON when Neo4j is down

**Then Implement:**
- [ ] Implement `memory_backfill() -> str` as `@tool(tags=["memory"])` that: 1) scans unclassified entries, 2) classifies all, 3) auto-assigns high-confidence, 4) sends review batches for the rest, 5) returns summary JSON
- [ ] Wire up the orchestration flow calling `_scan_unclassified_memories()`, `_scan_unclassified_entities()`, `_auto_assign_batch()`, `_send_review_batches()`

**Verify:** `pytest tests/unit/test_backfill_tool.py -v`

---

### Step 7: Fix Epilogue Source and Add Data Class to Epilogue Writes

**Files:**
- Modify: `core/epilogue.py` -- Fix `write_to_memory()` to pass valid source and infer data_class for each topic/entity
- Modify: `core/memory_schema.py` -- Add `"session_epilogue"` to `VALID_SOURCES` (or change epilogue to use `"session"`)

**Test First (unit):** `tests/unit/test_epilogue_write_classification.py`
- [ ] `test_epilogue_write_memory_uses_valid_source` -- `write_to_memory()` passes source="session" (not "session_epilogue")
- [ ] `test_epilogue_write_memory_defaults_data_class_to_session_log` -- topics from epilogue get data_class="session-log" by default
- [ ] `test_epilogue_write_entity_infers_data_class_from_type` -- entity with type="person" gets data_class="person"
- [ ] `test_epilogue_write_entity_type_preference_gets_preference_class` -- entity type "preference" gets data_class="preference"
- [ ] `test_epilogue_write_relationship_has_data_class` -- relationships written by epilogue include data_class on the edge

**Then Implement:**
- [ ] Change `core/epilogue.py` `write_to_memory()` line 327: `source="session_epilogue"` -> `source="session"`
- [ ] Add `data_class="session-log"` to the `memory_store_direct()` call for each topic
- [ ] Add `data_class` inference for entities: map entity type to data class using `_ENTITY_TYPE_MAP` -> data class (Person->person, Preference->preference, Project->technical-config, etc.)
- [ ] Pass inferred `data_class` to `graph_upsert_direct()` calls for entities and relationships

**Verify:** `pytest tests/unit/test_epilogue_write_classification.py -v`

---

### Step 8: Make data_class Required

**Files:**
- Modify: `agents/memory.py` -- Change `data_class: str | None = None` to `data_class: str` in both `memory_store` and `memory_store_direct`
- Modify: `agents/knowledge_graph.py` -- Change `data_class: str | None = None` to `data_class: str` in both `graph_upsert` and `graph_upsert_direct`
- Modify: `core/memory_schema.py` -- Update `validate_data_class()` to raise ValueError on None

**Test First (unit):** `tests/unit/test_data_class_required.py`
- [ ] `test_memory_store_without_data_class_raises` -- calling `memory_store(content="x")` without data_class raises TypeError (missing required arg)
- [ ] `test_memory_store_direct_without_data_class_raises` -- calling `memory_store_direct(content="x")` without data_class raises TypeError
- [ ] `test_graph_upsert_without_data_class_raises` -- calling `graph_upsert(entity_type="Person", name="X")` without data_class raises TypeError
- [ ] `test_graph_upsert_direct_without_data_class_raises` -- calling `graph_upsert_direct(entity_type="Person", name="X")` without data_class raises TypeError
- [ ] `test_validate_data_class_none_raises_value_error` -- `validate_data_class(None)` raises ValueError instead of returning None
- [ ] `test_build_metadata_requires_data_class` -- `build_metadata(data_class=None, source="user")` raises ValueError

**Then Implement:**
- [ ] Update `core/memory_schema.py` `validate_data_class()`: change the `if data_class is None` branch from warning+return None to raising ValueError
- [ ] Update `core/memory_schema.py` `build_metadata()`: let the ValueError from validate_data_class propagate
- [ ] Update `agents/memory.py`: change `data_class: str | None = None` to `data_class: str` in both function signatures
- [ ] Update `agents/knowledge_graph.py`: change `data_class: str | None = None` to `data_class: str` in both function signatures
- [ ] Update existing tests that relied on data_class=None backward compatibility: `tests/unit/test_memory_backward_compat.py`, `tests/unit/test_memory_store_metadata.py` (deprecation test), `tests/unit/test_graph_upsert_metadata.py` (deprecation test), `tests/integration/test_memory_metadata_flow.py` (epilogue compat test)

**Verify:** `pytest tests/ -v` -- all tests pass with new required-data_class behavior

---

### Step 9: Verification Tool -- Check All Entries Classified

**Files:**
- Modify: `agents/memory_backfill.py` -- Add `memory_backfill_status()` tool

**Test First (unit):** `tests/unit/test_backfill_status.py`
- [ ] `test_backfill_status_all_classified` -- when no NULL data_class nodes exist, returns {complete: true, unclassified: 0}
- [ ] `test_backfill_status_some_unclassified` -- when NULL data_class nodes exist, returns {complete: false, unclassified: N}
- [ ] `test_backfill_status_returns_class_distribution` -- returns count per data_class for audit

**Then Implement:**
- [ ] Implement `memory_backfill_status() -> str` as `@tool(tags=["memory"])` that queries for remaining unclassified entries and returns a status report with class distribution
- [ ] Add Cypher aggregation query: `MATCH (m:Memory) RETURN m.data_class AS class, count(*) AS count`

**Verify:** `pytest tests/unit/test_backfill_status.py -v`

---

### Step 10: Integration Test -- Full Backfill Flow

**Files:**
- Create: `tests/integration/test_backfill_flow.py` -- End-to-end backfill with mock Neo4j

**Test First (integration):** `tests/integration/test_backfill_flow.py`
- [ ] `test_full_backfill_classifies_all_memories` -- scan returns 5 unclassified memories, classifier assigns 3 high-confidence, 2 go to review queue
- [ ] `test_full_backfill_classifies_entities` -- scan returns entity nodes, all get classified based on entity type
- [ ] `test_backfill_then_status_shows_complete` -- after running backfill + applying all classifications, status shows 0 unclassified
- [ ] `test_epilogue_write_after_required_data_class` -- epilogue write_to_memory works with required data_class (no backward compat needed)

**Then Implement:**
- [ ] Create integration test file with mock Neo4j driver that simulates the full flow
- [ ] Verify end-to-end: scan -> classify -> assign -> review -> status

**Verify:** `pytest tests/integration/test_backfill_flow.py -v`

---

## Integration Checklist

- [ ] MCP tool `memory_backfill` decorated and discoverable in `agents/memory_backfill.py`
- [ ] MCP tool `memory_backfill_status` decorated and discoverable in `agents/memory_backfill.py`
- [ ] `/classify_*` command handler registered in `clients/telegram_bot.py`
- [ ] `core/backfill_classifier.py` importable with no side effects
- [ ] `core/backfill_review.py` importable with no side effects
- [ ] No new dependencies needed (uses existing neo4j, httpx, agent_tooling)
- [ ] `core/memory_schema.py` `DATA_CLASS_KEYWORDS` added for classifier
- [ ] `core/epilogue.py` `write_to_memory()` fixed to pass valid source and data_class
- [ ] `data_class` made required in `memory_store`, `memory_store_direct`, `graph_upsert`, `graph_upsert_direct`
- [ ] Existing tests updated for required data_class behavior
- [ ] `specs/memory-lifecycle.md` updated with any new classes discovered (manual, post-backfill)

## Build Verification

- [ ] `pytest -v` passes
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `ruff check .` passes
- [ ] All ACs addressed:
  - AC1: Backfill scans all entries missing data_class (Steps 2, 6)
  - AC2: Classification attempted against 7 classes (Step 1)
  - AC3: High-confidence auto-assign with metadata (Step 3)
  - AC4: Low-confidence queued for review (Steps 4, 6)
  - AC5: Telegram batch review flow (Step 4)
  - AC6: Summary and candidates shown (Step 4)
  - AC7: Responses applied immediately (Step 5)
  - AC8: New classes added to registry (Step 5)
  - AC9-10: All entries classified, none remain (Step 9)
  - AC11: New classes documented in spec (manual, post-backfill)
  - AC12: data_class required (Step 8)
