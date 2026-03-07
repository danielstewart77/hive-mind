# Implementation Plan: 1723686142072587807 - Monthly Review Pass

## Overview

Implement a monthly scheduler job that surfaces `world-event`, `intention`, and `session-log` memory entries for Daniel's review via Telegram. Daniel responds with keep/archive/discard per entry. This is the human-in-the-loop pass for data that cannot be auto-pruned (Pass 4 in `specs/memory-lifecycle.md`). Additionally, implement a JSON file-based archive store for world-events and update `memory_retrieve` to exclude archived entries by default.

## Technical Approach

Follow the existing sweep pattern established by `core/memory_expiry.py`, `core/orphan_sweep.py`, and `core/techconfig_pruning.py`:
- A core module (`core/monthly_review.py`) handles the Neo4j query and review logic
- A scheduler function in `clients/scheduler.py` calls a gateway endpoint
- A gateway endpoint in `server.py` delegates to the core module via `asyncio.to_thread`
- Review responses are handled via Telegram command-based pattern (`/keep_<id>`, `/archive_<id>`, `/discard_<id>`) matching the existing HITL `/approve_<token>` and backfill `/classify_<id>` patterns -- no inline keyboards, keeping the bot thin

Archive store design: A thin abstraction (`core/archive_store.py`) with a JSON file backend at `/data/world_events_archive.json`. The abstraction uses a simple class interface so the backend can be swapped to SQLite or a separate Neo4j label later without changing call sites.

The `memory_retrieve` tool gets an `include_archived` parameter. The Neo4j query adds a `WHERE m.archived IS NULL OR m.archived = false` filter by default.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|-------------|-------|
| Sweep module structure | `core/memory_expiry.py` | Lazy driver import, `_telegram_direct`, query + process loop, return summary dict |
| Scheduler integration | `clients/scheduler.py::_memory_expiry_sweep` | Async function calling gateway endpoint with HITL auth |
| API endpoint with auth | `server.py::memory_expiry_sweep` | HITL token check, `asyncio.to_thread` delegation |
| Telegram command handler | `clients/telegram_bot.py::cmd_classify` | Command regex handler for `/classify_<id>` |
| Unit test mock pattern | `tests/unit/test_memory_expiry_sweep.py` | Mock Neo4j driver, mock `_telegram_direct`, test sweep results |
| API test pattern | `tests/api/test_memory_expiry_sweep.py` | TestClient with patched config and session_mgr |
| Scheduler test pattern | `tests/unit/test_memory_expiry_scheduler.py` | Mock aiohttp session, test endpoint call and logging |

## Models & Schemas

### `core/archive_store.py` — ArchiveStore abstraction

```python
@dataclass
class ArchivedEntry:
    original_id: str          # Neo4j elementId at time of archive
    content: str
    data_class: str
    tags: str
    source: str
    agent_id: str
    created_at: int           # original created_at timestamp
    archived_at: str          # ISO 8601 datetime
    original_metadata: dict   # full original node properties

class ArchiveStore:
    def __init__(self, path: Path): ...
    def save(self, entry: ArchivedEntry) -> None: ...
    def list_all(self) -> list[ArchivedEntry]: ...
    def get(self, original_id: str) -> ArchivedEntry | None: ...
```

### `core/monthly_review.py` — Review entry shape

```python
@dataclass
class ReviewEntry:
    element_id: str
    content: str
    data_class: str
    created_at: int
    last_reviewed_at: int | None
```

## Implementation Steps

### Step 1: Archive Store Abstraction

**Files:**
- Create: `core/archive_store.py` -- JSON file-backed archive store for world-events

**Test First (unit):** `tests/unit/test_archive_store.py`
- [ ] `test_save_creates_file_if_not_exists` -- asserts saving to a non-existent file creates it and writes valid JSON
- [ ] `test_save_appends_to_existing_entries` -- asserts saving a second entry results in two entries in the file
- [ ] `test_list_all_returns_saved_entries` -- asserts list_all returns all previously saved entries
- [ ] `test_list_all_empty_file_returns_empty_list` -- asserts list_all returns [] when file is empty or missing
- [ ] `test_get_by_original_id_returns_entry` -- asserts get(id) returns the matching archived entry
- [ ] `test_get_by_original_id_not_found_returns_none` -- asserts get(id) returns None for unknown ID
- [ ] `test_save_writes_correct_schema_fields` -- asserts saved JSON contains archived_at, original_id, content, data_class, original_metadata
- [ ] `test_save_handles_concurrent_write_gracefully` -- asserts no data corruption on rapid sequential saves (file locking)

**Then Implement:**
- [ ] Create `core/archive_store.py` with `ArchivedEntry` dataclass and `ArchiveStore` class
- [ ] Implement JSON file read/write with `fcntl.flock` for file locking (following the pattern of atomicity in the codebase)
- [ ] Default path: `/usr/src/app/data/world_events_archive.json`
- [ ] `save()` reads existing JSON array, appends new entry, writes back
- [ ] `list_all()` reads and deserializes all entries
- [ ] `get(original_id)` scans the list for a matching `original_id`

**Verify:** `pytest tests/unit/test_archive_store.py -v`

---

### Step 2: Monthly Review Query Logic

**Files:**
- Create: `core/monthly_review.py` -- Core sweep module for monthly review

**Test First (unit):** `tests/unit/test_monthly_review.py`
- [ ] `test_query_returns_entries_due_for_review_null_last_reviewed` -- asserts entries with `last_reviewed_at=null` are returned
- [ ] `test_query_returns_entries_reviewed_over_30_days_ago` -- asserts entries where `last_reviewed_at` is more than 30 days ago are returned
- [ ] `test_query_excludes_entries_reviewed_within_30_days` -- asserts recently-reviewed entries are NOT returned (query returns empty)
- [ ] `test_query_filters_by_correct_data_classes` -- asserts only `world-event`, `intention`, `session-log` entries are returned
- [ ] `test_query_excludes_archived_entries` -- asserts entries with `archived=true` are NOT returned
- [ ] `test_entries_grouped_by_data_class` -- asserts the return dict has keys for each class with entries as values
- [ ] `test_empty_results_returns_empty_groups` -- asserts no error when no entries are due for review

**Then Implement:**
- [ ] Create `core/monthly_review.py` following `core/memory_expiry.py` pattern (lazy `_get_driver`, `_telegram_direct`)
- [ ] Implement `query_entries_for_review()` that runs Cypher query:
  ```
  MATCH (m:Memory)
  WHERE m.data_class IN ['world-event', 'intention', 'session-log']
    AND (m.archived IS NULL OR m.archived = false)
    AND (m.last_reviewed_at IS NULL OR m.last_reviewed_at < $cutoff)
  RETURN m.content, m.data_class, m.created_at, m.last_reviewed_at, elementId(m) AS id
  ```
- [ ] Group results into dict keyed by `data_class`

**Verify:** `pytest tests/unit/test_monthly_review.py -v`

---

### Step 3: Review Message Builder

**Files:**
- Modify: `core/monthly_review.py` -- Add message formatting function

**Test First (unit):** `tests/unit/test_monthly_review.py` (additional tests)
- [ ] `test_build_review_message_groups_by_class` -- asserts message contains class-based section headers
- [ ] `test_build_review_message_includes_entry_summary` -- asserts each entry's content (truncated) and date are in the message
- [ ] `test_build_review_message_includes_action_commands` -- asserts each entry has `/keep_<id>`, `/archive_<id>` (world-event only), `/discard_<id>` commands
- [ ] `test_build_review_message_archive_only_for_world_events` -- asserts `/archive_<id>` only appears for world-event entries, not for intention or session-log
- [ ] `test_build_review_message_truncates_long_content` -- asserts entries with very long content are truncated to a readable summary
- [ ] `test_build_review_message_single_message_per_class` -- asserts one message string is returned per class group

**Then Implement:**
- [ ] Implement `build_review_messages(grouped_entries: dict) -> dict[str, str]` that builds one Telegram message per class group
- [ ] Format: section header with class name, then for each entry: truncated content (first 200 chars), date stored (human-readable), and action commands
- [ ] `/keep_<short_id>` and `/discard_<short_id>` for all classes; `/archive_<short_id>` only for `world-event`
- [ ] Short ID: first 12 chars of the Neo4j element ID (sufficient for uniqueness in a batch)

**Verify:** `pytest tests/unit/test_monthly_review.py -v`

---

### Step 4: Response Handlers (Keep, Archive, Discard)

**Files:**
- Modify: `core/monthly_review.py` -- Add keep/archive/discard handler functions

**Test First (unit):** `tests/unit/test_monthly_review.py` (additional tests)
- [ ] `test_handle_keep_sets_last_reviewed_at` -- asserts Neo4j SET query updates `last_reviewed_at` to current timestamp
- [ ] `test_handle_keep_does_not_modify_other_fields` -- asserts only `last_reviewed_at` is changed
- [ ] `test_handle_archive_marks_archived_true` -- asserts Neo4j SET query sets `archived=true`
- [ ] `test_handle_archive_saves_to_archive_store` -- asserts `ArchiveStore.save()` is called with correct entry data
- [ ] `test_handle_archive_only_for_world_events` -- asserts archive returns error for non-world-event entries
- [ ] `test_handle_discard_deletes_from_neo4j` -- asserts DETACH DELETE Cypher is executed for the entry
- [ ] `test_handle_discard_returns_success` -- asserts the function returns a success result dict
- [ ] `test_handle_keep_unknown_id_returns_error` -- asserts error when element ID does not exist
- [ ] `test_handle_discard_unknown_id_does_not_raise` -- asserts no exception for missing entry (idempotent)

**Then Implement:**
- [ ] `handle_keep(element_id: str) -> dict` -- runs `SET m.last_reviewed_at = timestamp()` in Neo4j
- [ ] `handle_archive(element_id: str) -> dict` -- reads full entry from Neo4j, saves to `ArchiveStore`, then sets `m.archived = true` and `m.last_reviewed_at = timestamp()` in Neo4j
- [ ] `handle_discard(element_id: str) -> dict` -- runs `MATCH (m) WHERE elementId(m) = $id DETACH DELETE m` in Neo4j
- [ ] All handlers return `{"ok": True, "action": "..."}` on success or `{"ok": False, "error": "..."}` on failure

**Verify:** `pytest tests/unit/test_monthly_review.py -v`

---

### Step 5: Monthly Review Sweep (orchestrator)

**Files:**
- Modify: `core/monthly_review.py` -- Add `sweep_monthly_review()` orchestrator

**Test First (unit):** `tests/unit/test_monthly_review.py` (additional tests)
- [ ] `test_sweep_monthly_review_queries_and_sends_messages` -- asserts query + message build + Telegram send flow
- [ ] `test_sweep_monthly_review_no_entries_sends_nothing` -- asserts no Telegram call when no entries due for review
- [ ] `test_sweep_monthly_review_returns_summary_dict` -- asserts return dict has `entries_found`, `messages_sent`, `errors` keys
- [ ] `test_sweep_monthly_review_handles_telegram_failure_gracefully` -- asserts sweep completes even if Telegram send fails

**Then Implement:**
- [ ] `sweep_monthly_review() -> dict` that calls `query_entries_for_review()`, builds messages via `build_review_messages()`, sends each via `_telegram_direct()`
- [ ] Returns summary: `{"entries_found": N, "messages_sent": N, "errors": N}`
- [ ] Follow `sweep_expired_events()` pattern exactly for error handling

**Verify:** `pytest tests/unit/test_monthly_review.py -v`

---

### Step 6: Gateway Endpoint

**Files:**
- Modify: `server.py` -- Add `/memory/monthly-review` POST endpoint

**Test First (API):** `tests/api/test_monthly_review.py`
- [ ] `test_monthly_review_endpoint_returns_200` -- asserts 200 with valid auth token
- [ ] `test_monthly_review_rejects_missing_token` -- asserts 401 without auth header
- [ ] `test_monthly_review_rejects_wrong_token` -- asserts 401 with incorrect token
- [ ] `test_monthly_review_returns_summary_counts` -- asserts response body contains `entries_found`, `messages_sent`, `errors`
- [ ] `test_monthly_review_uses_to_thread` -- asserts `asyncio.to_thread` is used to avoid blocking the event loop

**Then Implement:**
- [ ] Add `POST /memory/monthly-review` endpoint to `server.py` following the exact pattern of `/memory/expiry-sweep`
- [ ] HITL token check, then `await asyncio.to_thread(sweep_monthly_review)`
- [ ] Lazy import: `from core.monthly_review import sweep_monthly_review`

**Verify:** `pytest tests/api/test_monthly_review.py -v`

---

### Step 7: Review Response Gateway Endpoint

**Files:**
- Modify: `server.py` -- Add `/memory/review-respond` POST endpoint

**Test First (API):** `tests/api/test_monthly_review.py` (additional tests)
- [ ] `test_review_respond_keep_returns_200` -- asserts 200 for keep action
- [ ] `test_review_respond_archive_returns_200` -- asserts 200 for archive action
- [ ] `test_review_respond_discard_returns_200` -- asserts 200 for discard action
- [ ] `test_review_respond_invalid_action_returns_400` -- asserts 400 for unknown action
- [ ] `test_review_respond_rejects_missing_token` -- asserts 401 without auth header
- [ ] `test_review_respond_returns_handler_result` -- asserts response body matches handler output

**Then Implement:**
- [ ] Add Pydantic model `ReviewRespondRequest` with fields: `element_id: str`, `action: str` (keep/archive/discard)
- [ ] Add `POST /memory/review-respond` endpoint with HITL auth check
- [ ] Route to `handle_keep`, `handle_archive`, or `handle_discard` based on action
- [ ] Return handler result dict

**Verify:** `pytest tests/api/test_monthly_review.py -v`

---

### Step 8: Scheduler Job (Monthly)

**Files:**
- Modify: `clients/scheduler.py` -- Add `_monthly_review_sweep()` function and cron job

**Test First (unit):** `tests/unit/test_monthly_review_scheduler.py`
- [ ] `test_monthly_review_sweep_calls_endpoint` -- asserts POST to `/memory/monthly-review` with auth header
- [ ] `test_monthly_review_sweep_logs_results` -- asserts sweep results are logged
- [ ] `test_monthly_review_sweep_handles_failure` -- asserts graceful handling of connection errors

**Then Implement:**
- [ ] Add `_monthly_review_sweep()` async function following `_memory_expiry_sweep()` pattern exactly
- [ ] Add CronTrigger in `main()`: 1st of every month at 9:00 AM CT (`hour="9", minute="0", day="1"`)
- [ ] Register with `scheduler.add_job()` with id `"monthly-review-sweep"`
- [ ] Update the log message for active jobs count

**Verify:** `pytest tests/unit/test_monthly_review_scheduler.py -v`

---

### Step 9: Telegram Bot Command Handlers for Review Responses

**Files:**
- Modify: `clients/telegram_bot.py` -- Add `/keep_<id>`, `/archive_<id>`, `/discard_<id>` handlers

**Test First (unit):** `tests/unit/test_monthly_review_telegram.py`
- [ ] `test_keep_command_calls_review_respond_endpoint` -- asserts the handler POSTs to `/memory/review-respond` with action=keep and correct element_id
- [ ] `test_archive_command_calls_review_respond_endpoint` -- asserts action=archive
- [ ] `test_discard_command_calls_review_respond_endpoint` -- asserts action=discard
- [ ] `test_review_command_extracts_id_from_command` -- asserts element ID is correctly parsed from `/keep_<id>`
- [ ] `test_review_command_invalid_format_replies_error` -- asserts error message for malformed command
- [ ] `test_review_command_unauthorized_user_rejected` -- asserts non-allowed users cannot use review commands

**Then Implement:**
- [ ] Add `cmd_review_keep`, `cmd_review_archive`, `cmd_review_discard` handlers
- [ ] Each extracts element ID from command text (e.g., `/keep_abc123` -> `abc123`)
- [ ] Each POSTs to `{SERVER_URL}/memory/review-respond` with `{"element_id": id, "action": "keep"}` and HITL auth header
- [ ] Register handlers using `MessageHandler(filters.Regex(r"^/keep_\w+$"), cmd_review_keep)` etc.
- [ ] Place before the catch-all handler in the handler list

**Verify:** `pytest tests/unit/test_monthly_review_telegram.py -v`

---

### Step 10: Update `memory_retrieve` to Exclude Archived Entries

**Files:**
- Modify: `agents/memory.py` -- Add `include_archived` parameter to `memory_retrieve`

**Test First (unit):** `tests/unit/test_memory_retrieve_archived.py`
- [ ] `test_memory_retrieve_excludes_archived_by_default` -- asserts the Cypher query includes `archived IS NULL OR archived = false` filter
- [ ] `test_memory_retrieve_includes_archived_when_flag_set` -- asserts the Cypher query does NOT filter on archived when `include_archived=True`
- [ ] `test_memory_retrieve_signature_has_include_archived_param` -- asserts the function signature includes `include_archived: bool = False`
- [ ] `test_memory_retrieve_returns_archived_field_in_results` -- asserts each result includes the `archived` field value

**Then Implement:**
- [ ] Add `include_archived: bool = False` parameter to `memory_retrieve`
- [ ] When `include_archived=False` (default), add `AND (m.archived IS NULL OR m.archived = false)` to both query variants (with and without tag_filter)
- [ ] When `include_archived=True`, omit the filter
- [ ] Include `m.archived AS archived` in the RETURN clause and add to result dicts

**Verify:** `pytest tests/unit/test_memory_retrieve_archived.py -v`

---

### Step 11: Integration Tests

**Files:**
- Create: `tests/integration/test_monthly_review_flow.py`

**Test First (integration):**
- [ ] `test_full_review_flow_keep_updates_last_reviewed_at` -- asserts keep handler + Neo4j mock shows SET query with timestamp
- [ ] `test_full_review_flow_archive_saves_and_marks` -- asserts archive handler writes to archive store AND marks archived=true in Neo4j
- [ ] `test_full_review_flow_discard_deletes_entry` -- asserts discard handler runs DETACH DELETE on the entry
- [ ] `test_monthly_review_sweep_to_telegram_message` -- asserts the sweep queries entries, builds messages, and sends via Telegram
- [ ] `test_archived_entry_excluded_from_memory_retrieve` -- asserts that after archiving, the entry no longer appears in default `memory_retrieve` results

**Then Implement:**
- [ ] Tests compose the sweep query, handler functions, and archive store in realistic combinations
- [ ] Use mock Neo4j driver and mock Telegram, same pattern as `tests/integration/test_memory_expiry_flow.py`

**Verify:** `pytest tests/integration/test_monthly_review_flow.py -v`

---

## Integration Checklist

- [ ] Route `POST /memory/monthly-review` registered in `server.py`
- [ ] Route `POST /memory/review-respond` registered in `server.py`
- [ ] Monthly cron job registered in `clients/scheduler.py` main()
- [ ] Telegram handlers for `/keep_*`, `/archive_*`, `/discard_*` registered in `clients/telegram_bot.py` (before catch-all)
- [ ] `memory_retrieve` updated with `include_archived` parameter in `agents/memory.py`
- [ ] Archive store at `/usr/src/app/data/world_events_archive.json` (auto-created on first write)
- [ ] No new dependencies needed in `requirements.txt` (all uses existing packages)
- [ ] No secrets needed (uses existing HITL token for auth)

## Build Verification

- [ ] `pytest tests/ -v` passes (all existing + new tests)
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `ruff check .` passes
- [ ] All ACs addressed:
  - AC1: Monthly scheduler job queries correct data classes with 30-day review window (Step 2, 8)
  - AC2: Batches entries by class and sends Telegram messages (Step 3, 5)
  - AC3: Review message grouped by class (Step 3)
  - AC4: Each entry has summary + date + options (Step 3)
  - AC5: Single message per class group (Step 3)
  - AC6: Keep sets last_reviewed_at (Step 4)
  - AC7: Archive moves to store, removes from active, marks archived (Step 4)
  - AC8: Discard deletes entry (Step 4)
  - AC9: Long-term archive store implemented (Step 1)
  - AC10: memory_retrieve excludes archived by default (Step 10)
  - AC11-15: Test coverage (Steps 1-11)
