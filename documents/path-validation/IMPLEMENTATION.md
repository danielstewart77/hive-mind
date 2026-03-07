# Implementation Plan: 1720154166069822499 - [Security MEDIUM-4] No Path Validation on Skill documents_path

## Overview

Three skill agent files (`skill_planning_genius.py`, `skill_code_genius.py`, `skill_code_review_genius.py`) pass `documents_path` directly to `subprocess.run()` without any validation. This CWE-22 path traversal vulnerability allows malicious or misconfigured paths (e.g., `../../.env`, `/etc/shadow`, symlinks) to be processed, potentially exposing or modifying sensitive files. The fix creates a reusable path validation function in `core/` and integrates it into all three skills.

## Technical Approach

Create a `validate_documents_path()` function in a new `core/path_validation.py` module. This follows the project's established pattern of placing shared utilities in `core/` (see `core/audit.py`, `core/tool_runner.py`). The validator will:

1. Use `os.path.realpath()` to resolve symlinks and canonicalize the path (per AC)
2. Verify the resolved path starts with the project's `documents/` directory (`PROJECT_DIR / "documents"`)
3. Raise a `ValueError` with a clear message when validation fails (per AC)
4. Be called at the top of each skill function before any `subprocess.run()` call

The validator uses `config.PROJECT_DIR` (already resolved via `Path.resolve()`) as the trusted base, ensuring consistency with the rest of the codebase.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|------------|-------|
| Core utility module | `/usr/src/app/core/audit.py` | Small, focused module in `core/` with clear exports. Tests in `tests/unit/test_audit.py` |
| Tool function structure | `/usr/src/app/agents/skill_planning_genius.py` | Skill wrapper pattern -- `@tool()` decorated, accepts path, calls `subprocess.run` |
| Unit test class style | `/usr/src/app/tests/unit/test_audit.py` | Grouped `TestClassName` with descriptive method names, `pytest.raises` for error cases |
| Integration test style | `/usr/src/app/tests/integration/test_pip_audit_integration.py` | Cross-module verification, import checks, round-trip validation |

## Models & Schemas

No new Pydantic models or dataclasses needed. The validator is a pure function:

```python
# core/path_validation.py
def validate_documents_path(documents_path: str) -> str:
    """Validate and canonicalize a documents_path, returning the resolved path.
    Raises ValueError if the path escapes the allowed documents/ directory."""
```

## Implementation Steps

### Step 1: Create `core/path_validation.py` with `validate_documents_path()`

**Files:**
- Create: `core/path_validation.py` -- reusable path validation utility

**Test First (unit):** `tests/unit/test_path_validation.py`

- [ ] `test_valid_path_within_documents_returns_resolved` -- pass a valid path like `/usr/src/app/documents/12345` and assert it returns the resolved path unchanged
- [ ] `test_valid_path_with_subdirectory_returns_resolved` -- pass `/usr/src/app/documents/12345/sub` and assert it returns successfully
- [ ] `test_relative_traversal_dot_dot_rejected` -- pass `/usr/src/app/documents/../.env` and assert `ValueError` is raised with a clear message
- [ ] `test_deep_traversal_rejected` -- pass `/usr/src/app/documents/../../etc/shadow` and assert `ValueError` is raised
- [ ] `test_absolute_path_outside_documents_rejected` -- pass `/etc/shadow` and assert `ValueError` is raised
- [ ] `test_absolute_path_to_env_rejected` -- pass `/usr/src/app/.env` and assert `ValueError` is raised
- [ ] `test_symlink_escape_rejected` -- create a temp symlink from inside `documents/` pointing to `/tmp`, pass the symlink path, assert `ValueError` is raised (uses `tmp_path` fixture)
- [ ] `test_empty_path_rejected` -- pass `""` and assert `ValueError` is raised
- [ ] `test_documents_dir_itself_rejected` -- pass the `documents/` directory itself (not a subdirectory) and assert `ValueError` is raised (must be a subdirectory of `documents/`, not documents itself)
- [ ] `test_path_with_null_byte_rejected` -- pass a path containing `\x00` and assert `ValueError` is raised
- [ ] `test_error_message_does_not_leak_resolved_path` -- verify the error message says "outside allowed directory" but does not contain the full resolved path (prevents information leakage)
- [ ] `test_return_type_is_string` -- assert the return value is a `str`

**Then Implement:**
- [ ] Create `core/path_validation.py` following the module docstring pattern from `core/audit.py`
- [ ] Import `PROJECT_DIR` from `config` (same pattern as `core/sessions.py` line 22)
- [ ] Define `DOCUMENTS_DIR = PROJECT_DIR / "documents"` as a module constant
- [ ] Implement `validate_documents_path(documents_path: str) -> str`:
  1. Reject empty strings and null-byte-containing strings immediately
  2. Call `os.path.realpath(documents_path)` to resolve symlinks and canonicalize
  3. Compute `str(DOCUMENTS_DIR) + os.sep` as the required prefix
  4. Check that the resolved path starts with the required prefix (ensures it is a subdirectory, not `documents/` itself)
  5. If check fails, raise `ValueError("documents_path is outside the allowed documents/ directory")`
  6. Return the resolved path string

**Verify:** `pytest tests/unit/test_path_validation.py -v` -- all 12 tests should pass.

---

### Step 2: Integrate validation into `agents/skill_planning_genius.py`

**Files:**
- Modify: `agents/skill_planning_genius.py` -- add validation call before `subprocess.run()`

**Test First (unit):** `tests/unit/test_skill_path_validation.py`

- [ ] `test_planning_genius_rejects_traversal_path` -- call `planning_genius("../../../etc/shadow")`, assert it returns an error string containing "outside the allowed" (not a stack trace)
- [ ] `test_planning_genius_rejects_absolute_sensitive_path` -- call `planning_genius("/etc/passwd")`, assert error string returned
- [ ] `test_planning_genius_valid_path_calls_subprocess` -- mock `subprocess.run`, call with a valid documents path, assert `subprocess.run` was called (path reaches subprocess)

**Then Implement:**
- [ ] Add `from core.path_validation import validate_documents_path` to imports
- [ ] Add a try/except block at the top of `planning_genius()` before the `subprocess.run()` call:
  ```python
  try:
      documents_path = validate_documents_path(documents_path)
  except ValueError as e:
      return f"Path validation failed: {e}"
  ```
- [ ] The `subprocess.run()` call now receives the canonicalized path from the validator

**Verify:** `pytest tests/unit/test_skill_path_validation.py -v -k planning` -- planning tests pass.

---

### Step 3: Integrate validation into `agents/skill_code_genius.py`

**Files:**
- Modify: `agents/skill_code_genius.py` -- add validation call before `subprocess.run()`

**Test First (unit):** `tests/unit/test_skill_path_validation.py` (append to same file)

- [ ] `test_code_genius_rejects_traversal_path` -- call `code_genius("../../../etc/shadow")`, assert error string returned
- [ ] `test_code_genius_rejects_absolute_sensitive_path` -- call `code_genius("/etc/passwd")`, assert error string returned
- [ ] `test_code_genius_valid_path_calls_subprocess` -- mock `subprocess.run`, call with valid documents path, assert subprocess was called

**Then Implement:**
- [ ] Add `from core.path_validation import validate_documents_path` to imports
- [ ] Add the same try/except validation block at the top of `code_genius()`:
  ```python
  try:
      documents_path = validate_documents_path(documents_path)
  except ValueError as e:
      return f"Path validation failed: {e}"
  ```

**Verify:** `pytest tests/unit/test_skill_path_validation.py -v -k code_genius` -- code genius tests pass.

---

### Step 4: Integrate validation into `agents/skill_code_review_genius.py`

**Files:**
- Modify: `agents/skill_code_review_genius.py` -- add validation call before `subprocess.run()`

**Test First (unit):** `tests/unit/test_skill_path_validation.py` (append to same file)

- [ ] `test_code_review_genius_rejects_traversal_path` -- call `code_review_genius("../../../etc/shadow")`, assert error string returned
- [ ] `test_code_review_genius_rejects_absolute_sensitive_path` -- call `code_review_genius("/etc/passwd")`, assert error string returned
- [ ] `test_code_review_genius_valid_path_calls_subprocess` -- mock `subprocess.run`, call with valid documents path, assert subprocess was called

**Then Implement:**
- [ ] Add `from core.path_validation import validate_documents_path` to imports
- [ ] Add the same try/except validation block at the top of `code_review_genius()`:
  ```python
  try:
      documents_path = validate_documents_path(documents_path)
  except ValueError as e:
      return f"Path validation failed: {e}"
  ```

**Verify:** `pytest tests/unit/test_skill_path_validation.py -v -k code_review` -- code review tests pass.

---

### Step 5: Integration test confirming all three skills reject malicious paths gracefully

**Files:**
- Create: `tests/integration/test_skill_path_traversal.py` -- cross-module integration test

**Test First (integration):** `tests/integration/test_skill_path_traversal.py`

- [ ] `test_all_skills_reject_dot_dot_traversal` -- for each of the three skill functions, call with `../../.env` and assert all return error strings (not exceptions)
- [ ] `test_all_skills_reject_absolute_escape` -- for each skill, call with `/etc/shadow` and assert error strings returned
- [ ] `test_all_skills_accept_valid_documents_path` -- for each skill, mock `subprocess.run` to return success, call with a real subdirectory under `documents/`, assert subprocess was called (path accepted)
- [ ] `test_symlink_attack_blocked_across_all_skills` -- create a symlink from `documents/evil` to `/tmp` using `tmp_path`, for each skill, assert the symlink path is rejected (uses monkeypatch to override DOCUMENTS_DIR to a temp dir to make symlink test hermetic)
- [ ] `test_validation_module_imports_cleanly` -- import `core.path_validation` and verify `validate_documents_path` is callable

**Then Implement:**
- [ ] Create the test file following the pattern from `tests/integration/test_pip_audit_integration.py` (class-based, docstrings, `unittest.mock.patch`)
- [ ] Use `@pytest.fixture` for temporary directory setup where needed
- [ ] Import all three skill functions and call them directly with malicious paths
- [ ] For valid-path tests, mock `subprocess.run` to avoid actually spawning `claude` CLI

**Verify:** `pytest tests/integration/test_skill_path_traversal.py -v` -- all integration tests pass.

---

### Step 6: Full test suite verification

**Files:** None (verification only)

**Verify:**
- [ ] `pytest tests/ -v` -- entire test suite passes with no regressions
- [ ] `ruff check core/path_validation.py agents/skill_planning_genius.py agents/skill_code_genius.py agents/skill_code_review_genius.py` -- no lint issues
- [ ] `mypy core/path_validation.py --ignore-missing-imports` -- no type errors

---

## Integration Checklist

- [ ] Routes registered in `server.py` -- N/A (no new routes)
- [ ] MCP tools decorated and discoverable in `agents/` -- N/A (existing tools modified, not added)
- [ ] Config additions in `config.py` / `config.yaml` -- N/A (uses existing `PROJECT_DIR`)
- [ ] Dependencies added to `requirements.txt` -- N/A (only stdlib used: `os`, `os.path`)
- [ ] Secrets stored in keyring (not env/code) -- N/A (no new secrets)

## Build Verification

- [ ] `pytest -v` passes
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `ruff check .` passes
- [ ] All ACs addressed:
  - [AC1] Path validation added to all three skill agent files (Steps 2-4)
  - [AC2] `os.path.realpath()` used to resolve symlinks and canonicalize paths (Step 1 implementation)
  - [AC3] Validated that `documents_path` is within expected `documents/` directory (Step 1 prefix check)
  - [AC4] Paths outside the allowed directory are rejected with clear error messages (Step 1 ValueError, Steps 2-4 catch and return)
  - [AC5] Unit tests cover valid paths and attack vectors (Step 1 unit tests + Steps 2-4 unit tests)
  - [AC6] Integration test confirms skills reject malicious paths gracefully (Step 5)
