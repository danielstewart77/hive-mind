# Implementation Plan: 1735358468149217106 - mind_id flows through gateway + session manager

## Overview

Wire `mind_id` through the full stack so the gateway and session manager can spawn the correct subprocess per mind. The default mind is `"ada"`, ensuring full backward compatibility with all existing clients. This involves three layers: config (add `minds` dict to `HiveMindConfig`), API (add optional `mind_id` field to `CreateSessionRequest`), and session manager (look up mind config, use per-mind soul file path, store `mind_id` in the DB).

## Technical Approach

- Follow the existing migration pattern in `SessionManager.start()` (the `epilogue_status` ALTER TABLE wrapped in try/except) for the new `mind_id` column.
- Follow the existing `CreateSessionRequest` Pydantic model pattern in `server.py` for the new optional field.
- Follow the existing `_SOUL_FILE` usage in `_build_base_prompt()` and `_spawn()` for soul file parameterization.
- The `_SOUL_FILE` constant remains as the default fallback for Ada (and when no mind config is found).
- `config.minds` is a plain `dict[str, dict]` loaded from YAML -- no new dataclass needed, matching the existing `providers` pattern.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|-------------|-------|
| DB column migration (try/except ALTER TABLE) | `core/sessions.py` lines 187-193 | Pattern for adding `mind_id` column |
| Optional request field with default | `server.py` `CreateSessionRequest.model` field | Pattern for `mind_id: str = "ada"` |
| Config dict field (providers) | `config.py` `HiveMindConfig.providers` | Pattern for `minds: dict` field |
| `_session_dict` output shape | `core/sessions.py` lines 719-735 | Pattern for including `mind_id` in response |
| Existing test: `test_session_schema.py` | `tests/unit/test_session_schema.py` | Pattern for testing schema + `_session_dict` |
| Existing API test: `test_hitl_inline_buttons.py` | `tests/api/test_hitl_inline_buttons.py` | Pattern for API endpoint testing with `TestClient` |

## Models & Schemas

**Modified: `config.py` `HiveMindConfig`**
- Add field: `minds: dict = field(default_factory=dict)` -- loaded from `_yaml_config.get("minds", {})`

**Modified: `server.py` `CreateSessionRequest`**
- Add field: `mind_id: str = "ada"` -- optional, defaults to `"ada"`

**No new Pydantic models or dataclasses required.**

## Implementation Steps

Each step: write test first, then implement to pass.

### Step 1: Add `minds` field to `HiveMindConfig`

**Files:**
- Modify: `config.py` -- add `minds: dict` field and load it from YAML

**Test First (unit):** `tests/unit/test_config_minds.py`
- [ ] `test_config_has_minds_field` -- asserts `config.minds` exists and is a dict
- [ ] `test_config_minds_contains_ada` -- asserts `config.minds` has an `"ada"` key with `"soul"` and `"backend"` subkeys (validates actual config.yaml loading)
- [ ] `test_config_minds_default_empty_dict` -- asserts that `HiveMindConfig()` (no YAML) has `minds` as `{}`

**Then Implement:**
- [ ] In `config.py` `HiveMindConfig` dataclass, add `minds: dict = field(default_factory=dict)` after the `models` field
- [ ] In `HiveMindConfig.from_yaml()`, add `minds=_yaml_config.get("minds", {}),` to the constructor call

**Verify:** `pytest tests/unit/test_config_minds.py -v` -- all tests pass.

---

### Step 2: Add `mind_id` column to sessions DB (migration)

**Files:**
- Modify: `core/sessions.py` -- add `mind_id TEXT DEFAULT 'ada'` to `_SCHEMA` and add migration in `start()`

**Test First (unit):** `tests/unit/test_session_mind_id.py`
- [ ] `test_schema_includes_mind_id_column` -- asserts `"mind_id"` appears in `_SCHEMA` string from `core.sessions`
- [ ] `test_schema_mind_id_has_default_ada` -- asserts `"mind_id TEXT DEFAULT 'ada'"` appears in `_SCHEMA`

**Then Implement:**
- [ ] In `core/sessions.py` `_SCHEMA`, add `mind_id TEXT DEFAULT 'ada'` column to the `sessions` CREATE TABLE statement, after the `epilogue_status` column
- [ ] In `SessionManager.start()`, add a migration block (following the `epilogue_status` pattern) that does `ALTER TABLE sessions ADD COLUMN mind_id TEXT DEFAULT 'ada'` wrapped in try/except

**Verify:** `pytest tests/unit/test_session_mind_id.py -v` -- all tests pass.

---

### Step 3: Accept `mind_id` in `create_session()` and store it in DB

**Files:**
- Modify: `core/sessions.py` -- add `mind_id` parameter to `create_session()`, include in INSERT, and expose in `_session_dict()`

**Test First (unit):** `tests/unit/test_session_mind_id.py` (append to file from Step 2)
- [ ] `test_session_dict_includes_mind_id` -- mock `_get_row` to return a row with `mind_id: "ada"`, call `_session_dict()`, assert `result["mind_id"] == "ada"` (follows pattern in `test_session_schema.py::TestSessionDictIncludesEpilogueStatus`)
- [ ] `test_session_dict_includes_mind_id_nagatha` -- same but with `mind_id: "nagatha"`, assert `result["mind_id"] == "nagatha"`

**Then Implement:**
- [ ] Add `mind_id: str = "ada"` parameter to `create_session()` signature (after `allowed_directories`)
- [ ] Update the INSERT statement in `create_session()` to include `mind_id` in the columns and values
- [ ] Update `_session_dict()` to include `"mind_id": row["mind_id"]` in the returned dict (following the `epilogue_status` pattern using `.get()` for migration safety: `row.get("mind_id", "ada")`)

**Verify:** `pytest tests/unit/test_session_mind_id.py -v` -- all tests pass.

---

### Step 4: Wire mind config lookup into `_build_base_prompt()` and `_spawn()`

This is the core change: when a session is created with a mind_id, look up that mind's config from `config.minds`, and use the mind's soul file path instead of the hardcoded `_SOUL_FILE`.

**Files:**
- Modify: `core/sessions.py` -- parameterize `_build_base_prompt()` to accept a soul_file path; update `_spawn()` to accept and pass `mind_id`; update `create_session()` to do the config lookup and pass soul path to `_spawn()`

**Test First (unit):** `tests/unit/test_session_mind_id.py` (append)
- [ ] `test_build_base_prompt_uses_custom_soul_path` -- patch `_fetch_soul_sync` to return `None` so the fallback branch executes; call `_build_base_prompt(soul_file=Path("/tmp/test_soul.md"))` and assert the returned string contains `"/tmp/test_soul.md"` (the fallback soul instruction references the path)
- [ ] `test_build_base_prompt_default_soul_file` -- call `_build_base_prompt()` with no `soul_file` argument; patch `_fetch_soul_sync` to return `None`; assert the returned string contains the default `_SOUL_FILE` path (backward compat)
- [ ] `test_create_session_looks_up_mind_config` -- integration-style test using a real aiosqlite in-memory DB: instantiate `SessionManager`, call `start()`, mock `_spawn()`, mock `config.minds` to `{"ada": {"soul": "souls/ada.md", "backend": "cli_claude", "model": "sonnet"}}`, call `create_session(mind_id="ada")`, assert `_spawn()` was called with `soul_file` matching `PROJECT_DIR / "souls/ada.md"`
- [ ] `test_create_session_unknown_mind_falls_back_to_ada` -- same setup but call `create_session(mind_id="unknown_mind")`, assert `_spawn()` was called with the default `_SOUL_FILE` path (or the ada config path if ada config exists)

**Then Implement:**
- [ ] Modify `_build_base_prompt()` signature to accept `soul_file: Path | None = None`. Inside the function, use `soul_file or _SOUL_FILE` as the effective soul path. The only place `_SOUL_FILE` is referenced in this function is in the graph-unavailable fallback (`f"Read {_SOUL_FILE} at the start..."`). Change it to use the effective soul path variable.
- [ ] Modify `_spawn()` signature to accept `soul_file: Path | None = None`. Pass it through to `_build_base_prompt(soul_file=soul_file, allowed_directories=allowed_directories)`.
- [ ] In `create_session()`, after resolving the model, add the mind config lookup:
  ```
  mind_cfg = config.minds.get(mind_id, {})
  soul_rel = mind_cfg.get("soul")
  soul_file = PROJECT_DIR / soul_rel if soul_rel else None
  ```
  Then pass `soul_file=soul_file` to `self._spawn()`.
- [ ] `_SOUL_FILE` constant remains unchanged as the default fallback.

**Verify:** `pytest tests/unit/test_session_mind_id.py -v` -- all tests pass.

---

### Step 5: Add `mind_id` to `CreateSessionRequest` and wire through `server.py`

**Files:**
- Modify: `server.py` -- add `mind_id: str = "ada"` to `CreateSessionRequest`, pass to `session_mgr.create_session()`

**Test First (API):** `tests/api/test_session_mind_id.py`
- [ ] `test_create_session_without_mind_id_defaults_to_ada` -- POST `/sessions` with no `mind_id` field; assert `session_mgr.create_session` was called with `mind_id="ada"` (mock `session_mgr`)
- [ ] `test_create_session_with_mind_id_passes_through` -- POST `/sessions` with `mind_id="nagatha"`; assert `session_mgr.create_session` was called with `mind_id="nagatha"`
- [ ] `test_create_session_response_includes_mind_id` -- POST `/sessions` with `mind_id="ada"`; mock `session_mgr.create_session` to return a dict containing `mind_id`; assert response JSON includes `"mind_id": "ada"`

**Then Implement:**
- [ ] In `server.py` `CreateSessionRequest`, add field: `mind_id: str = "ada"`
- [ ] In `server.py` `create_session()` endpoint, pass `mind_id=body.mind_id` to `session_mgr.create_session()`
- [ ] In `server.py` `_handle_command()` for `/new` and `/clear`, the existing `create_session()` call does not pass `mind_id`, so it will default to `"ada"` -- this is correct backward-compatible behavior. No change needed here.

**Verify:** `pytest tests/api/test_session_mind_id.py -v` -- all tests pass.

---

### Step 6: End-to-end integration test with real SQLite

**Files:**
- Create: `tests/integration/test_session_mind_id_flow.py` -- full flow through session manager with real DB

**Test First (integration):** `tests/integration/test_session_mind_id_flow.py`
- [ ] `test_create_session_stores_mind_id_in_db` -- create a `SessionManager` with real aiosqlite (temp file), call `start()`, mock `_spawn()`, mock `config.minds`, call `create_session(mind_id="ada")`, then query DB directly to assert `mind_id = 'ada'` in the sessions row
- [ ] `test_create_session_default_mind_id_in_db` -- same but call `create_session()` without `mind_id`, assert DB row has `mind_id = 'ada'`
- [ ] `test_session_dict_returns_mind_id` -- after creating a session, call `get_session()` and assert the returned dict has `"mind_id"` key
- [ ] `test_migration_adds_mind_id_to_existing_db` -- create a DB with the old schema (no `mind_id` column), then call `start()`, assert the column now exists by inserting a row and reading `mind_id` back

**Then Implement:**
- No new production code -- this step verifies the integration of Steps 2-5.

**Verify:** `pytest tests/integration/test_session_mind_id_flow.py -v` -- all tests pass.

---

## Integration Checklist

- [N/A] Routes registered in `server.py` -- no new routes; existing POST `/sessions` modified
- [N/A] MCP tools decorated and discoverable in `agents/` -- no MCP changes
- [ ] Config additions in `config.py` / `config.yaml` -- `minds` field added to `HiveMindConfig`
- [N/A] Dependencies added to `requirements.txt` -- no new dependencies
- [N/A] Secrets stored in keyring -- no new secrets

## Build Verification

- [ ] `pytest -v` passes (all existing + new tests)
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `ruff check .` passes
- [ ] AC: POST /sessions with no mind_id behaves identically to today (Step 5 test)
- [ ] AC: POST /sessions with mind_id='ada' spawns Ada's subprocess with souls/ada.md (Step 4 test)
- [ ] AC: sessions table has mind_id column populated on new sessions (Step 6 integration test)
- [ ] AC: No existing functionality broken (all existing tests still pass)
