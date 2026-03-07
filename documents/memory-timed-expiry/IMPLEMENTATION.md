# Implementation Plan: 1723685753000560150 - Timed-Event Auto-Expiry -- Nightly Pass

## Overview

Implement the first automated memory pruning pass: timed-event expiry. At write time, `memory_store` and `memory_store_direct` will validate `timed-event` entries have a resolved absolute `expires_at` datetime and detect recurring events via keyword heuristics. A nightly scheduler job will query Neo4j for expired `timed-event` entries, delete non-recurring ones unconditionally, and send Telegram prompts for recurring ones asking Daniel whether to keep or delete.

## Technical Approach

1. **Write-time validation** extends `core/memory_schema.py` (`build_metadata`) to validate `expires_at` is a parseable ISO datetime and to detect/set the `recurring` boolean flag. The `agents/memory.py` module already stores `expires_at` in Neo4j -- we add `recurring` as a new node property.

2. **Recurring detection** is a pure function in `core/memory_schema.py` using keyword heuristics (birthday, anniversary, weekly, monthly, annual, every, recurring) against the `content` field. An explicit `recurring=True` parameter overrides the heuristic.

3. **Nightly expiry job** is a new module `core/memory_expiry.py` containing the query-and-delete logic. It uses the Neo4j driver directly (same pattern as `agents/memory.py`), queries `timed-event` nodes where `expires_at < now`, deletes non-recurring entries, and sends Telegram prompts for recurring ones via `agents/notify.py` (`_telegram_direct`).

4. **Scheduler integration** adds a new job in `clients/scheduler.py` calling the expiry sweep, following the same pattern as `_epilogue_sweep`.

5. **Gateway endpoint** adds `POST /memory/expiry-sweep` (auth-gated like `/epilogue/sweep`) so the scheduler can trigger the sweep over HTTP, keeping the pattern consistent.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|-------------|-------|
| Metadata validation + build | `core/memory_schema.py` | Extend `build_metadata` for `recurring`, validate `expires_at` format |
| Neo4j driver mocking | `tests/unit/test_memory_store_metadata.py` | `_make_mock_driver()`, `patch.object(mem_mod, ...)` |
| Scheduler job registration | `clients/scheduler.py` (`_epilogue_sweep`) | Add `_memory_expiry_sweep` following same pattern |
| Gateway auth-gated endpoint | `server.py` (`/epilogue/sweep`) | Add `/memory/expiry-sweep` with `X-HITL-Internal` header |
| API endpoint testing | `tests/api/test_epilogue_sweep.py` | TestClient with patched config/session_mgr |
| Telegram notification | `agents/notify.py` (`_telegram_direct`) | Send recurring event prompts to Daniel |
| Unit test conftest | `tests/unit/conftest.py` | Mock neo4j, agent_tooling, keyring at module level |

## Models & Schemas

### Changes to `core/memory_schema.py`

- Add `RECURRING_KEYWORDS: frozenset[str]` -- `{"birthday", "anniversary", "weekly", "monthly", "annual", "every", "recurring"}`
- Add function `detect_recurring(content: str) -> bool` -- scans content for keyword matches (case-insensitive word boundary matching)
- Add function `validate_expires_at(expires_at: str) -> str` -- validates ISO 8601 datetime, returns normalized string, raises `ValueError` on parse failure
- Modify `build_metadata()` to accept optional `recurring: bool | None` and `content: str | None` params; for `timed-event`, validate `expires_at` format and compute `recurring` flag

### Changes to `agents/memory.py`

- Add `recurring` parameter to `memory_store` and `memory_store_direct`
- Store `recurring` as a Neo4j node property on Memory nodes
- Add `recurring` index in `_ensure_index`

### New module: `core/memory_expiry.py`

- `sweep_expired_events() -> dict` -- queries Neo4j for expired timed-events, deletes non-recurring, sends Telegram for recurring, returns summary counts
- Uses the same Neo4j driver pattern as `agents/memory.py`
- Logs all deletions via standard Python logging

### Changes to `server.py`

- Add `POST /memory/expiry-sweep` endpoint (auth-gated with `X-HITL-Internal`)

### Changes to `clients/scheduler.py`

- Add `_memory_expiry_sweep()` async function
- Register it on a nightly cron trigger (e.g., `0 3 * * *`)

## Implementation Steps

Each step: write test first, then implement to pass.
Tests assert observable behavior only (return values, API responses, raised exceptions) -- never internal state, private methods, or implementation details.

### Step 1: Recurring keyword detection function

**Files:**
- Modify: `core/memory_schema.py` -- add `RECURRING_KEYWORDS` constant and `detect_recurring(content: str) -> bool` function

**Test First (unit):** `tests/unit/test_memory_expiry.py`
- [ ] `test_detect_recurring_birthday_returns_true` -- asserts `detect_recurring("Mom's birthday party")` returns `True`
- [ ] `test_detect_recurring_anniversary_returns_true` -- asserts `detect_recurring("Wedding anniversary dinner")` returns `True`
- [ ] `test_detect_recurring_weekly_returns_true` -- asserts `detect_recurring("Weekly standup meeting")` returns `True`
- [ ] `test_detect_recurring_monthly_returns_true` -- asserts `detect_recurring("Monthly review")` returns `True`
- [ ] `test_detect_recurring_annual_returns_true` -- asserts `detect_recurring("Annual performance review")` returns `True`
- [ ] `test_detect_recurring_every_returns_true` -- asserts `detect_recurring("Every Tuesday yoga class")` returns `True`
- [ ] `test_detect_recurring_keyword_recurring_returns_true` -- asserts `detect_recurring("Recurring team sync")` returns `True`
- [ ] `test_detect_recurring_no_keywords_returns_false` -- asserts `detect_recurring("Doctor appointment tomorrow")` returns `False`
- [ ] `test_detect_recurring_case_insensitive` -- asserts `detect_recurring("BIRTHDAY celebration")` returns `True`
- [ ] `test_detect_recurring_partial_word_no_match` -- asserts `detect_recurring("I am annually reviewing")` returns `True` (word boundary match includes "annually" since "annual" is a substring that should match at word boundary)
- [ ] `test_detect_recurring_empty_string` -- asserts `detect_recurring("")` returns `False`

**Then Implement:**
- [ ] Add `RECURRING_KEYWORDS = frozenset({"birthday", "anniversary", "weekly", "monthly", "annual", "every", "recurring"})` to `core/memory_schema.py`
- [ ] Add `detect_recurring(content: str) -> bool` that uses `re.search` with word boundary pattern `r'\b(?:birthday|anniversary|weekly|monthly|annual|every|recurring)\b'` case-insensitive

**Verify:** `pytest tests/unit/test_memory_expiry.py -v -k "detect_recurring"` -- all detection tests pass.

---

### Step 2: Validate `expires_at` ISO format

**Files:**
- Modify: `core/memory_schema.py` -- add `validate_expires_at(expires_at: str) -> str` function

**Test First (unit):** `tests/unit/test_memory_expiry.py`
- [ ] `test_validate_expires_at_valid_iso_returns_string` -- asserts `validate_expires_at("2026-04-01T15:00:00Z")` returns the string without error
- [ ] `test_validate_expires_at_with_timezone_offset` -- asserts `validate_expires_at("2026-04-01T15:00:00-05:00")` returns valid string
- [ ] `test_validate_expires_at_no_timezone_returns_string` -- asserts `validate_expires_at("2026-04-01T15:00:00")` returns valid string (bare datetime accepted)
- [ ] `test_validate_expires_at_invalid_format_raises` -- asserts `validate_expires_at("next Tuesday at 3pm")` raises `ValueError` with message about unresolved time reference
- [ ] `test_validate_expires_at_relative_time_raises` -- asserts `validate_expires_at("tomorrow")` raises `ValueError`
- [ ] `test_validate_expires_at_empty_string_raises` -- asserts `validate_expires_at("")` raises `ValueError`
- [ ] `test_validate_expires_at_date_only_raises` -- asserts `validate_expires_at("2026-04-01")` raises `ValueError` (date without time is ambiguous for an event)

**Then Implement:**
- [ ] Add `validate_expires_at(expires_at: str) -> str` to `core/memory_schema.py`. Uses `datetime.fromisoformat(expires_at)` to validate. On `ValueError`, raise with message: "expires_at must be a resolved absolute ISO datetime (e.g. '2026-04-01T15:00:00Z'). Relative or unresolved time references like '{expires_at}' are not valid. Please resolve to an absolute datetime, reclassify the entry, or discard."
- [ ] Date-only strings (no 'T' separator) should be rejected as ambiguous.

**Verify:** `pytest tests/unit/test_memory_expiry.py -v -k "validate_expires_at"` -- all validation tests pass.

---

### Step 3: Integrate `recurring` and `expires_at` validation into `build_metadata`

**Files:**
- Modify: `core/memory_schema.py` -- extend `build_metadata` signature to accept `recurring: bool | None = None` and `content: str | None = None`; for `timed-event`, call `validate_expires_at` and compute `recurring` flag

**Test First (unit):** `tests/unit/test_memory_expiry.py`
- [ ] `test_build_metadata_timed_event_validates_expires_format` -- asserts `build_metadata(data_class="timed-event", source="user", expires_at="next Tuesday")` raises `ValueError`
- [ ] `test_build_metadata_timed_event_valid_expires_passes` -- asserts `build_metadata(data_class="timed-event", source="user", expires_at="2026-04-01T15:00:00Z")` returns dict with `expires_at` and `recurring` fields
- [ ] `test_build_metadata_timed_event_recurring_from_content` -- asserts `build_metadata(data_class="timed-event", source="user", expires_at="2026-04-01T15:00:00Z", content="Mom's birthday dinner")` returns `{"recurring": True, ...}`
- [ ] `test_build_metadata_timed_event_not_recurring_by_default` -- asserts `build_metadata(data_class="timed-event", source="user", expires_at="2026-04-01T15:00:00Z", content="Doctor appointment")` returns `{"recurring": False, ...}`
- [ ] `test_build_metadata_timed_event_explicit_recurring_override` -- asserts `build_metadata(data_class="timed-event", source="user", expires_at="2026-04-01T15:00:00Z", content="Doctor appointment", recurring=True)` returns `{"recurring": True, ...}`
- [ ] `test_build_metadata_non_timed_event_ignores_recurring` -- asserts `build_metadata(data_class="person", source="user")` does not contain `recurring` key
- [ ] `test_build_metadata_timed_event_no_content_defaults_recurring_false` -- asserts that when `content` is `None`, `recurring` defaults to `False` (unless explicit `recurring=True`)

**Then Implement:**
- [ ] Add `recurring: bool | None = None` and `content: str | None = None` parameters to `build_metadata`
- [ ] For `timed-event` class: call `validate_expires_at(expires_at)` before storing; compute `recurring` as `recurring if recurring is not None else detect_recurring(content or "")`; add `"recurring"` to the metadata dict
- [ ] For non-timed-event classes: do not add `recurring` to metadata

**Verify:** `pytest tests/unit/test_memory_expiry.py -v -k "build_metadata"` -- all metadata tests pass. Also run `pytest tests/unit/test_memory_schema.py -v` to ensure existing tests still pass (backward compatibility).

---

### Step 4: Add `recurring` parameter to `memory_store` and `memory_store_direct`

**Files:**
- Modify: `agents/memory.py` -- add `recurring: bool | None = None` parameter to both functions; pass `recurring` and `content` through to `build_metadata`; store `recurring` as Neo4j node property
- Modify: `agents/memory.py` -- add `recurring` index in `_ensure_index`

**Test First (unit):** `tests/unit/test_memory_expiry.py`
- [ ] `test_memory_store_direct_timed_event_with_recurring_stores_property` -- asserts that the Neo4j Cypher params include `recurring=True` when content contains "birthday"
- [ ] `test_memory_store_direct_timed_event_explicit_recurring_false` -- asserts Cypher params include `recurring=False` when `recurring=False` passed explicitly even with keyword content
- [ ] `test_memory_store_direct_timed_event_without_expires_returns_error` -- asserts result `{"stored": False}` (existing AC, confirm still works)
- [ ] `test_memory_store_direct_timed_event_invalid_expires_returns_error` -- asserts result `{"stored": False}` when `expires_at="next Friday"`
- [ ] `test_memory_store_direct_non_timed_event_no_recurring_property` -- asserts Cypher params do not include `recurring` for `data_class="person"`
- [ ] `test_memory_store_timed_event_hitl_approved_stores_recurring` -- asserts HITL-gated path passes `recurring` through

**Then Implement:**
- [ ] Add `recurring: bool | None = None` parameter to `memory_store_direct` and `memory_store`
- [ ] In `memory_store_direct`, pass `recurring=recurring` and `content=content` to `build_metadata`
- [ ] In the Cypher CREATE query, add `recurring: $recurring` property
- [ ] Pass `meta.get("recurring", False)` as the `recurring` parameter to `session.run`
- [ ] In `_ensure_index`, add `"recurring"` to the indexed fields list
- [ ] In `memory_store`, pass `recurring=recurring` to `memory_store_direct`

**Verify:** `pytest tests/unit/test_memory_expiry.py -v -k "memory_store"` -- all store tests pass. Also `pytest tests/unit/test_memory_store_metadata.py -v` for backward compat.

---

### Step 5: Nightly expiry sweep core logic

**Files:**
- Create: `core/memory_expiry.py` -- contains `sweep_expired_events()` function that queries Neo4j for expired timed-events and processes them

**Test First (unit):** `tests/unit/test_memory_expiry_sweep.py`
- [ ] `test_sweep_deletes_expired_non_recurring_events` -- mock Neo4j to return 2 expired non-recurring entries; asserts they are deleted via `DELETE` Cypher; asserts return counts `{"deleted": 2, "prompted": 0, "errors": 0}`
- [ ] `test_sweep_prompts_for_expired_recurring_events` -- mock Neo4j to return 1 expired recurring entry; mock `_telegram_direct`; asserts Telegram message sent with event content/date; asserts return counts `{"deleted": 0, "prompted": 1, "errors": 0}`
- [ ] `test_sweep_mixed_expired_events` -- mock returns 2 non-recurring + 1 recurring; asserts `{"deleted": 2, "prompted": 1, "errors": 0}`
- [ ] `test_sweep_no_expired_events` -- mock returns empty result; asserts `{"deleted": 0, "prompted": 0, "errors": 0}`
- [ ] `test_sweep_logs_deletions` -- mock Neo4j returns 1 expired entry; assert logging output includes deletion message (use `caplog` fixture)
- [ ] `test_sweep_handles_neo4j_error_gracefully` -- mock Neo4j to raise exception; asserts return `{"deleted": 0, "prompted": 0, "errors": 1}` and no unhandled exception
- [ ] `test_sweep_telegram_failure_does_not_block` -- mock Telegram send to fail; asserts sweep still completes and recurring entry is not deleted

**Then Implement:**
- [ ] Create `core/memory_expiry.py` following patterns from `agents/memory.py` for Neo4j driver access
- [ ] Import `_get_driver`, `_ensure_index` from `agents.memory` (or duplicate the lazy singleton pattern to avoid circular imports -- prefer importing)
- [ ] `sweep_expired_events()`: query `MATCH (m:Memory) WHERE m.data_class = 'timed-event' AND m.expires_at IS NOT NULL AND m.expires_at < $now RETURN m.content, m.expires_at, m.recurring, elementId(m) AS id`
- [ ] For each result: if `recurring` is `False` or `None`, delete the node (`MATCH (m) WHERE elementId(m) = $id DETACH DELETE m`); if `recurring` is `True`, send Telegram prompt via `agents.notify._telegram_direct`
- [ ] Log each deletion with `logger.info`
- [ ] Return summary dict `{"deleted": N, "prompted": N, "errors": N}`
- [ ] Compare `expires_at` as ISO string against `datetime.now(timezone.utc).isoformat()` -- Neo4j stores as string, so lexicographic comparison works for ISO 8601

**Verify:** `pytest tests/unit/test_memory_expiry_sweep.py -v` -- all sweep tests pass.

---

### Step 6: Gateway endpoint for expiry sweep

**Files:**
- Modify: `server.py` -- add `POST /memory/expiry-sweep` endpoint (same auth pattern as `/epilogue/sweep`)

**Test First (API):** `tests/api/test_memory_expiry_sweep.py`
- [ ] `test_expiry_sweep_endpoint_returns_200` -- asserts 200 with valid auth header and mocked sweep function
- [ ] `test_expiry_sweep_rejects_missing_token` -- asserts 401 without auth header
- [ ] `test_expiry_sweep_rejects_wrong_token` -- asserts 401 with wrong auth header
- [ ] `test_expiry_sweep_returns_counts` -- asserts response body contains `deleted`, `prompted`, `errors` keys

**Then Implement:**
- [ ] Add import of `sweep_expired_events` from `core.memory_expiry` in `server.py`
- [ ] Add endpoint following `/epilogue/sweep` pattern:
  ```python
  @app.post("/memory/expiry-sweep")
  async def memory_expiry_sweep(x_hitl_internal: str = Header(None)):
      if not config.hitl_internal_token:
          return JSONResponse({"error": "HITL not configured"}, status_code=500)
      if x_hitl_internal != config.hitl_internal_token:
          return JSONResponse({"error": "unauthorized"}, status_code=401)
      results = sweep_expired_events()
      return results
  ```

**Verify:** `pytest tests/api/test_memory_expiry_sweep.py -v` -- all API tests pass.

---

### Step 7: Scheduler integration

**Files:**
- Modify: `clients/scheduler.py` -- add `_memory_expiry_sweep()` async function and register it on a nightly cron trigger

**Test First (unit):** `tests/unit/test_memory_expiry_scheduler.py`
- [ ] `test_memory_expiry_sweep_calls_endpoint` -- mock aiohttp session; asserts `POST /memory/expiry-sweep` is called with correct auth header
- [ ] `test_memory_expiry_sweep_logs_results` -- mock endpoint response; asserts log output includes deletion/prompt counts
- [ ] `test_memory_expiry_sweep_handles_failure` -- mock endpoint to return 500; asserts no unhandled exception and error is logged

**Then Implement:**
- [ ] Add `_memory_expiry_sweep()` async function in `clients/scheduler.py` following `_epilogue_sweep` pattern:
  ```python
  async def _memory_expiry_sweep() -> None:
      log.info("Running memory expiry sweep")
      try:
          timeout = aiohttp.ClientTimeout(total=120)
          headers = {"X-HITL-Internal": config.hitl_internal_token or ""}
          async with aiohttp.ClientSession(timeout=timeout) as http:
              async with http.post(f"{SERVER_URL}/memory/expiry-sweep", headers=headers) as resp:
                  data = await resp.json()
                  log.info(
                      "Memory expiry sweep: deleted=%d, prompted=%d, errors=%d",
                      data.get("deleted", 0),
                      data.get("prompted", 0),
                      data.get("errors", 0),
                  )
      except Exception:
          log.exception("Memory expiry sweep failed")
  ```
- [ ] In `main()`, register the job: `scheduler.add_job(_memory_expiry_sweep, CronTrigger(hour="3", minute="30", timezone="America/Chicago"), id="memory-expiry-sweep")`
- [ ] Add log line: `log.info("Scheduled memory expiry sweep @ 30 3 * * *")`

**Verify:** `pytest tests/unit/test_memory_expiry_scheduler.py -v` -- all scheduler tests pass.

---

### Step 8: End-to-end integration test

**Files:**
- Create: `tests/integration/test_memory_expiry_flow.py` -- integration test verifying the full flow from expired entries to deletion/Telegram prompt

**Test First (integration):** `tests/integration/test_memory_expiry_flow.py`
- [ ] `test_expired_non_recurring_entry_is_deleted` -- set up mock Neo4j with an expired non-recurring timed-event node; call `sweep_expired_events()`; assert the node was deleted via Cypher DELETE
- [ ] `test_expired_recurring_entry_triggers_telegram` -- set up mock Neo4j with an expired recurring timed-event; mock `_telegram_direct`; call `sweep_expired_events()`; assert Telegram message sent with event content; assert node was NOT deleted
- [ ] `test_memory_store_rejects_unresolved_expires_at` -- call `memory_store_direct` with `data_class="timed-event"` and `expires_at="next Friday"`; assert error response
- [ ] `test_memory_store_sets_recurring_from_content` -- call `memory_store_direct` with content containing "birthday"; mock Neo4j; assert stored node has `recurring=True`

**Then Implement:**
- [ ] No new production code -- these tests exercise the code from Steps 1-7

**Verify:** `pytest tests/integration/test_memory_expiry_flow.py -v` -- all integration tests pass.

---

## Integration Checklist

- [ ] Routes registered in `server.py` (`POST /memory/expiry-sweep`)
- [ ] Scheduler job registered in `clients/scheduler.py` (nightly at 3:30 AM CT)
- [ ] `recurring` property added to Neo4j Memory nodes in `agents/memory.py`
- [ ] `recurring` index added in `_ensure_index` in `agents/memory.py`
- [ ] `detect_recurring` and `validate_expires_at` functions added to `core/memory_schema.py`
- [ ] `core/memory_expiry.py` created with `sweep_expired_events()` function
- [ ] No new dependencies required (uses existing Neo4j driver, httpx/aiohttp, and notify module)
- [ ] No secrets changes needed
- [ ] Config additions: none (scheduler cron is hardcoded like epilogue sweep)

## Build Verification

- [ ] `pytest -v` passes
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `ruff check .` passes
- [ ] All ACs addressed:
  - AC1: `memory_store` rejects `timed-event` without resolved `expires_at` -- Steps 2-4
  - AC2: Invalid time references trigger error prompting reclassification -- Step 2
  - AC3: `recurring` boolean flag set on timed-event entries -- Steps 1, 3, 4
  - AC4: Nightly scheduler job queries expired `timed-event` entries -- Steps 5, 7
  - AC5: Non-recurring entries deleted unconditionally -- Step 5
  - AC6: Recurring entries trigger Telegram prompt -- Step 5
  - AC7: All deletions logged -- Step 5
  - AC8: Recurring events detected via keyword heuristics -- Step 1
  - AC9: Manual override via explicit `recurring=True` -- Steps 3, 4
  - AC10: Unit tests for nightly job deletion -- Step 5
  - AC11: Unit tests for Telegram prompt on recurring -- Step 5
  - AC12: Unit tests for `memory_store` rejection -- Steps 2, 4
