# Implementation Plan: 1723686012946744860 - Technical-Config Pruning

## Overview

Implement a scheduled pruning pass for `technical-config` memory entries that verifies each stored fact against the codebase using lightweight heuristics (file/symbol existence checks via `os.path` and `subprocess`-based grep). Entries with a `codebase_ref` field are checked directly; entries without one are fuzzy-matched against the project file structure. Results are reported to Daniel via Telegram.

## Technical Approach

This follows the established sweep pattern used by `core/memory_expiry.py` and `core/orphan_sweep.py`:

1. **Core sweep module** (`core/techconfig_pruning.py`) -- queries Neo4j for all `data_class=technical-config` entries, runs verification heuristics, marks/deletes inaccurate entries, and sends a Telegram summary.
2. **Gateway endpoint** (`POST /memory/techconfig-sweep`) -- HITL-token-protected endpoint in `server.py` that calls the sweep via `asyncio.to_thread`.
3. **Scheduler job** (`clients/scheduler.py`) -- weekly cron job calling the gateway endpoint.
4. **Heuristic verifier** (`core/techconfig_verifier.py`) -- the actual verification logic, separated from the sweep orchestration for testability. Uses `os.path.exists()`, `os.path.isfile()`, and `subprocess.run(["grep", ...])` for symbol checks. No Claude session spawning.

**Design decisions:**
- **Weekly schedule** (not nightly) -- technical-config entries change infrequently; weekly is sufficient and avoids unnecessary Neo4j load.
- **Lightweight heuristics only** -- per the open question resolution: file existence + symbol grep. No Claude sessions.
- **Escalate to Daniel review** when: file referenced in `codebase_ref` does not exist, symbol search is ambiguous (multiple matches), or no `codebase_ref` and content cannot be mapped to any file.
- **`codebase_ref` is an optional string field** on Memory nodes -- a relative path from project root (e.g. `core/sessions.py`, `server.py`). Added to the Neo4j CREATE query in `agents/memory.py` but not enforced at schema level.
- **Pruned entries are marked `superseded=True`** rather than deleted, following the durable-update pattern from `specs/memory-lifecycle.md`.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|-------------|-------|
| Sweep module structure | `/usr/src/app/core/memory_expiry.py` | Lazy imports, `_get_driver()`, `_telegram_direct()`, return dict with counts |
| Sweep module structure | `/usr/src/app/core/orphan_sweep.py` | Same pattern, batch Telegram notification |
| API endpoint auth | `/usr/src/app/server.py` (lines 209-218) | HITL token check, `asyncio.to_thread` |
| Scheduler job | `/usr/src/app/clients/scheduler.py` (lines 150-166) | `_memory_expiry_sweep` pattern |
| Unit test structure | `/usr/src/app/tests/unit/test_memory_expiry_sweep.py` | Mock driver, mock Telegram, test counts |
| API test structure | `/usr/src/app/tests/api/test_memory_expiry_sweep.py` | TestClient, auth header, mock config |
| Scheduler test | `/usr/src/app/tests/unit/test_memory_expiry_scheduler.py` | Mock aiohttp, mock config, async tests |

## Models & Schemas

### No new Pydantic models needed.

The `codebase_ref` field is added as an optional string property on Neo4j Memory nodes. It is passed through existing `memory_store` / `memory_store_direct` calls via a new optional `codebase_ref` parameter.

### VerificationResult (internal dataclass in `core/techconfig_verifier.py`)

```python
@dataclass
class VerificationResult:
    status: str          # "verified" | "pruned" | "flagged"
    reason: str          # Human-readable explanation
    content: str         # Original memory content
    element_id: str      # Neo4j element ID
    codebase_ref: str | None  # Original codebase_ref if any
```

## Implementation Steps

### Step 1: Add `codebase_ref` field to memory store

**Files:**
- Modify: `/usr/src/app/agents/memory.py` -- add `codebase_ref: str | None = None` param to `memory_store`, `memory_store_direct`; include in CREATE Cypher and RETURN query
- Modify: `/usr/src/app/core/memory_schema.py` -- add `codebase_ref` to `build_metadata` output (passthrough, no validation needed)

**Test First (unit):** `tests/unit/test_techconfig_codebase_ref.py`
- [ ] `test_memory_store_direct_accepts_codebase_ref` -- asserts that passing `codebase_ref="server.py"` results in the Cypher query receiving the param
- [ ] `test_memory_store_direct_codebase_ref_defaults_to_none` -- asserts that omitting `codebase_ref` passes `None` to Cypher
- [ ] `test_memory_store_codebase_ref_passed_through` -- asserts that `memory_store` passes `codebase_ref` to `memory_store_direct`
- [ ] `test_memory_retrieve_returns_codebase_ref` -- asserts that retrieved memories include the `codebase_ref` field

**Then Implement:**
- [ ] Add `codebase_ref: str | None = None` parameter to `memory_store_direct()` and `memory_store()` in `/usr/src/app/agents/memory.py`
- [ ] Add `codebase_ref: $codebase_ref` to the CREATE Cypher in `memory_store_direct()`
- [ ] Add `codebase_ref=codebase_ref` to the `session.run()` params
- [ ] Add `m.codebase_ref AS codebase_ref` to both RETURN queries in `memory_retrieve()`
- [ ] Add `"codebase_ref": record["codebase_ref"]` to the memories list comprehension in `memory_retrieve()`

**Verify:** `pytest tests/unit/test_techconfig_codebase_ref.py -v`

---

### Step 2: Implement the verification heuristic module

**Files:**
- Create: `/usr/src/app/core/techconfig_verifier.py` -- contains `VerificationResult` dataclass and `verify_entry()` function

**Test First (unit):** `tests/unit/test_techconfig_verifier.py`
- [ ] `test_verify_entry_file_exists_and_symbol_found_returns_verified` -- entry with `codebase_ref="server.py"` and content mentioning "FastAPI" (symbol exists in file) returns status="verified"
- [ ] `test_verify_entry_file_exists_but_symbol_not_found_returns_pruned` -- entry with `codebase_ref="server.py"` and content mentioning a function that does not exist returns status="pruned"
- [ ] `test_verify_entry_file_does_not_exist_returns_flagged` -- entry with `codebase_ref="nonexistent.py"` returns status="flagged" (escalate to Daniel)
- [ ] `test_verify_entry_no_codebase_ref_infers_file_and_verifies` -- entry without `codebase_ref` but with content like "server.py handles /sessions" infers the file and verifies
- [ ] `test_verify_entry_no_codebase_ref_no_file_match_returns_flagged` -- entry without `codebase_ref` and no file inference possible returns status="flagged"
- [ ] `test_verify_entry_no_codebase_ref_file_inferred_but_symbol_missing_returns_pruned` -- entry without `codebase_ref`, file inferred from content, but symbol not found returns status="pruned"
- [ ] `test_verify_entry_empty_content_returns_flagged` -- edge case: empty content string
- [ ] `test_extract_keywords_from_content` -- tests the keyword extraction helper that pulls filenames, function names, and config keys from content text

**Then Implement:**
- [ ] Create `/usr/src/app/core/techconfig_verifier.py`
- [ ] Define `VerificationResult` dataclass with fields: `status`, `reason`, `content`, `element_id`, `codebase_ref`
- [ ] Implement `_extract_file_references(content: str) -> list[str]` -- regex to find file paths (e.g. `server.py`, `core/sessions.py`, `config.yaml`) in content text
- [ ] Implement `_extract_keywords(content: str) -> list[str]` -- extract likely symbol names (function names, class names, config keys) from content
- [ ] Implement `_check_file_exists(filepath: str) -> bool` -- `os.path.isfile(PROJECT_DIR / filepath)`
- [ ] Implement `_check_symbol_in_file(filepath: str, symbol: str) -> bool` -- uses `subprocess.run(["grep", "-q", symbol, str(PROJECT_DIR / filepath)])` with `shell=False`
- [ ] Implement `_check_symbol_in_project(symbol: str) -> bool` -- uses `subprocess.run(["grep", "-rq", symbol, str(PROJECT_DIR)])` with `shell=False`, for entries without `codebase_ref`
- [ ] Implement `verify_entry(content: str, element_id: str, codebase_ref: str | None) -> VerificationResult` -- the main decision tree:
  - If `codebase_ref` present and file exists: grep for keywords in file -> verified or pruned
  - If `codebase_ref` present and file missing: flagged
  - If `codebase_ref` absent: try to infer file from content, then grep keywords; if no inference possible, flagged

**Verify:** `pytest tests/unit/test_techconfig_verifier.py -v`

---

### Step 3: Implement the sweep orchestration module

**Files:**
- Create: `/usr/src/app/core/techconfig_pruning.py` -- queries Neo4j, calls verifier, marks entries, sends Telegram report

**Test First (unit):** `tests/unit/test_techconfig_pruning.py`
- [ ] `test_sweep_queries_technical_config_entries` -- asserts the Cypher query filters by `data_class='technical-config'`
- [ ] `test_sweep_verified_entry_not_modified` -- entry verified by heuristic is not changed in Neo4j; verified count incremented
- [ ] `test_sweep_pruned_entry_marked_superseded` -- entry that fails verification gets `superseded=True` set via Cypher UPDATE
- [ ] `test_sweep_flagged_entry_not_modified` -- entry that is flagged for review is not changed in Neo4j; flagged count incremented
- [ ] `test_sweep_sends_telegram_summary_with_counts` -- after sweep, Telegram is called with a message containing verified/pruned/flagged counts
- [ ] `test_sweep_sends_flagged_entries_in_review_message` -- flagged entries are batched into a single Telegram message with their content
- [ ] `test_sweep_no_telegram_when_nothing_found` -- no entries found means no Telegram message sent
- [ ] `test_sweep_neo4j_error_handled_gracefully` -- Neo4j failure does not raise; errors count incremented
- [ ] `test_sweep_telegram_failure_does_not_raise` -- Telegram failure handled gracefully
- [ ] `test_sweep_returns_result_dict` -- return dict has keys: verified, pruned, flagged, errors
- [ ] `test_sweep_empty_results_returns_zeros` -- no matching entries returns all-zero counts

**Then Implement:**
- [ ] Create `/usr/src/app/core/techconfig_pruning.py` following the pattern of `core/memory_expiry.py`
- [ ] Add lazy `_get_driver()` import from `agents.memory`
- [ ] Add lazy `_telegram_direct()` import from `agents.notify`
- [ ] Implement `sweep_techconfig_entries() -> dict`:
  - Query Neo4j: `MATCH (m:Memory) WHERE m.data_class = 'technical-config' AND (m.superseded IS NULL OR m.superseded = false) RETURN m.content, m.codebase_ref, elementId(m) AS id`
  - For each record, call `verify_entry()` from `core/techconfig_verifier.py`
  - If status="pruned": run `MATCH (m) WHERE elementId(m) = $id SET m.superseded = true`
  - Collect results into verified/pruned/flagged lists
  - Build summary message and send via Telegram
  - If flagged entries exist, send a second batch message listing them
  - Return `{"verified": N, "pruned": N, "flagged": N, "errors": N}`

**Verify:** `pytest tests/unit/test_techconfig_pruning.py -v`

---

### Step 4: Add the gateway endpoint

**Files:**
- Modify: `/usr/src/app/server.py` -- add `POST /memory/techconfig-sweep` endpoint

**Test First (API):** `tests/api/test_techconfig_sweep.py`
- [ ] `test_techconfig_sweep_endpoint_returns_200` -- valid auth token returns 200
- [ ] `test_techconfig_sweep_rejects_missing_token` -- missing token returns 401
- [ ] `test_techconfig_sweep_rejects_wrong_token` -- wrong token returns 401
- [ ] `test_techconfig_sweep_returns_result_counts` -- response JSON contains verified, pruned, flagged, errors keys

**Then Implement:**
- [ ] Add endpoint in `server.py` following the `memory_expiry_sweep` pattern (lines 209-218):
  ```python
  @app.post("/memory/techconfig-sweep")
  async def memory_techconfig_sweep(x_hitl_internal: str = Header(None)):
      if not config.hitl_internal_token:
          return JSONResponse({"error": "HITL not configured"}, status_code=500)
      if x_hitl_internal != config.hitl_internal_token:
          return JSONResponse({"error": "unauthorized"}, status_code=401)
      from core.techconfig_pruning import sweep_techconfig_entries
      results = await asyncio.to_thread(sweep_techconfig_entries)
      return results
  ```

**Verify:** `pytest tests/api/test_techconfig_sweep.py -v`

---

### Step 5: Add the scheduler job

**Files:**
- Modify: `/usr/src/app/clients/scheduler.py` -- add `_techconfig_sweep()` async function and register weekly cron job

**Test First (unit):** `tests/unit/test_techconfig_sweep_scheduler.py`
- [ ] `test_techconfig_sweep_calls_endpoint` -- asserts `_techconfig_sweep` POSTs to `/memory/techconfig-sweep` with auth header
- [ ] `test_techconfig_sweep_logs_results` -- asserts sweep results (verified, pruned, flagged) are logged
- [ ] `test_techconfig_sweep_handles_failure` -- asserts graceful handling when endpoint is unreachable

**Then Implement:**
- [ ] Add `_techconfig_sweep()` in `clients/scheduler.py` following `_memory_expiry_sweep()` pattern (lines 150-166):
  ```python
  async def _techconfig_sweep() -> None:
      log.info("Running technical-config pruning sweep")
      try:
          timeout = aiohttp.ClientTimeout(total=300)
          headers = {"X-HITL-Internal": config.hitl_internal_token or ""}
          async with aiohttp.ClientSession(timeout=timeout) as http:
              async with http.post(f"{SERVER_URL}/memory/techconfig-sweep", headers=headers) as resp:
                  data = await resp.json()
                  log.info(
                      "Techconfig sweep: verified=%d, pruned=%d, flagged=%d, errors=%d",
                      data.get("verified", 0),
                      data.get("pruned", 0),
                      data.get("flagged", 0),
                      data.get("errors", 0),
                  )
      except Exception:
          log.exception("Technical-config pruning sweep failed")
  ```
- [ ] Register weekly cron job in `main()` -- Sunday at 4:00 AM CT:
  ```python
  techconfig_trigger = CronTrigger(hour="4", minute="0", day_of_week="sun", timezone="America/Chicago")
  scheduler.add_job(_techconfig_sweep, techconfig_trigger, id="techconfig-sweep")
  log.info("Scheduled technical-config pruning sweep @ 0 4 * * 0")
  ```
- [ ] Update the `log.info` line in `main()` to include the new sweep in the job count message

**Verify:** `pytest tests/unit/test_techconfig_sweep_scheduler.py -v`

---

### Step 6: Integration test -- end-to-end sweep flow

**Files:**
- Create: `tests/integration/test_techconfig_pruning_flow.py`

**Test First (integration):** `tests/integration/test_techconfig_pruning_flow.py`
- [ ] `test_sweep_verifies_accurate_entry_end_to_end` -- creates a mock Memory node with `codebase_ref="server.py"` and content referencing an existing function, runs sweep, asserts node is NOT marked superseded
- [ ] `test_sweep_prunes_inaccurate_entry_end_to_end` -- creates a mock Memory node with `codebase_ref="server.py"` and content referencing a non-existent function, runs sweep, asserts node IS marked superseded
- [ ] `test_sweep_flags_unresolvable_entry_end_to_end` -- creates a mock Memory node with no `codebase_ref` and ambiguous content, runs sweep, asserts it is flagged (not modified, not pruned)
- [ ] `test_sweep_sends_summary_telegram` -- runs sweep with mixed results, asserts Telegram was called with correct summary counts

**Then Implement:** (tests only -- implementation done in previous steps)

**Verify:** `pytest tests/integration/test_techconfig_pruning_flow.py -v`

---

## Integration Checklist

- [ ] Route `POST /memory/techconfig-sweep` registered in `server.py`
- [ ] Scheduler job registered in `clients/scheduler.py` (weekly, Sunday 4 AM CT)
- [ ] `codebase_ref` field supported in `agents/memory.py` (memory_store, memory_store_direct, memory_retrieve)
- [ ] No new dependencies needed (uses `os.path`, `subprocess`, `re` -- all stdlib)
- [ ] No config additions needed (uses existing HITL token)
- [ ] No secrets needed (reuses existing Neo4j and Telegram credentials)

## Build Verification

- [ ] `pytest -v` passes
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `ruff check .` passes
- [ ] All ACs addressed:
  - AC1: Pruning job runs weekly via scheduler
  - AC2: Verification logic with `codebase_ref` (direct file check + fuzzy match)
  - AC3: Telegram summary with verified/pruned/flagged counts + flagged entries batch
  - AC4: Lightweight heuristic (file/symbol existence, no Claude session)
  - AC5: Tests for accurate kept, inaccurate pruned, no codebase_ref handled, summary sent
