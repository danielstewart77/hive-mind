# Code Review: 1720154169131664449 - Expand Audit Logging to All MCP Tool Invocations

## Summary

Clean, well-structured implementation that creates a centralised audit module (`core/audit.py`) with JSON formatting, secret redaction, and log rotation, integrates it into `mcp_server.py` to wrap all tool invocations, and migrates `tool_creator.py` from a manual `FileHandler` to the shared `RotatingFileHandler`. Tests are thorough, covering redaction, formatting, wrapping behaviour, and integration. No critical issues found.

**Verdict:** APPROVED WITH MINOR FIXES

## Acceptance Criteria Coverage

| # | Criterion | Status | Covered By |
|---|-----------|--------|------------|
| 1 | All MCP tool invocations logged with tool name, timestamp, args summary, result status | Implemented and tested | `core/audit.py:audit_wrap()`, `mcp_server.py:29`, `tests/unit/test_audit.py:TestAuditWrap`, `tests/unit/test_mcp_audit_integration.py` |
| 2 | Structured JSON (NDJSON) | Implemented and tested | `core/audit.py:JSONAuditFormatter`, `tests/unit/test_audit.py:TestJSONAuditFormatter` |
| 3 | Log rotation in place | Implemented and tested | `core/audit.py:get_audit_logger()` (5MB max, 3 backups), `tests/unit/test_audit.py:TestGetAuditLogger`, `tests/unit/test_tool_creator_audit.py` |
| 4 | No secrets in audit log | Implemented and tested | `core/audit.py:redact_args()` with `SENSITIVE_PARAMS`, `tests/unit/test_audit.py:TestRedactArgs` |
| 5 | Existing audit logging in tool_creator.py preserved and consistent | Implemented and tested | `agents/tool_creator.py:30` uses `get_audit_logger()`, existing `_audit.info/warning` calls preserved, `tests/unit/test_tool_creator_audit.py` |

## Files Reviewed

| File | Status | Findings |
|------|--------|----------|
| `core/audit.py` | New file | 2 minor, 1 nit |
| `mcp_server.py` | Modified | Clean |
| `agents/tool_creator.py` | Modified | Clean |
| `tests/unit/test_audit.py` | New file | 1 minor |
| `tests/unit/test_mcp_audit_integration.py` | New file | 1 minor |
| `tests/unit/test_tool_creator_audit.py` | New file | Clean |

## Findings

### Critical

None.

### Major

None.

### Minor

#### M1: Exception messages may leak sensitive data into audit log

- **File:** `/usr/src/app/core/audit.py:146`
- **Dimension:** Security
- **Description:** `error_msg = str(exc)` logs the full exception message. If an exception contains sensitive information (e.g., a database driver including a connection string with credentials, or an HTTP library including an Authorization header in its error), that data would appear in the audit log in plaintext. While this is unlikely for most MCP tools, it is a gap in the "no secrets in audit log" guarantee.
- **Suggested Fix:** Truncate error messages to a reasonable length (e.g., 200 chars) to reduce exposure surface. A more thorough approach would be to scan the error string for patterns matching known secret formats, but truncation alone is a pragmatic improvement.

#### M2: `audit_wrap` only handles synchronous functions

- **File:** `/usr/src/app/core/audit.py:115-162`
- **Dimension:** Maintainability
- **Description:** `audit_wrap` calls `func(*args, **kwargs)` synchronously. Currently all MCP tool functions in `agents/` are synchronous, so this works. However, if any tool is ever defined as `async def`, the wrapper would return a coroutine object instead of awaiting it, silently breaking the tool. The wrapper does not detect or handle this case.
- **Suggested Fix:** Add a guard at the top of `audit_wrap` that checks `inspect.iscoroutinefunction(func)` and either wraps with an async equivalent or raises a clear error. This is a forward-looking concern, not an active bug.

#### M3: `test_get_audit_logger_default_path` creates a real file in the project directory

- **File:** `/usr/src/app/tests/unit/test_audit.py:159-162`
- **Dimension:** TDD Test Coverage
- **Description:** This test calls `get_audit_logger()` with no arguments, which defaults to `<project>/audit.log`. This creates (or opens for append) a real file in the project root. Tests should avoid side effects on the filesystem outside temporary directories. It also risks adding a handler to the shared logger that persists across other tests.
- **Suggested Fix:** Use a `tempfile.TemporaryDirectory` like the other logger tests, or mock the handler creation. At minimum, clean up the handler after the test.

#### M4: MCP integration test does not clean up `sys.modules`

- **File:** `/usr/src/app/tests/unit/test_mcp_audit_integration.py:30-52`
- **Dimension:** TDD Test Coverage
- **Description:** The test pops `mcp_server` from `sys.modules` before the test but does not remove it after the test completes. The re-imported `mcp_server` module (with mocked dependencies) remains in `sys.modules`, which could interfere with other tests that import `mcp_server` in the same pytest session.
- **Suggested Fix:** Add a `finally` block or use `pytest`'s `monkeypatch.syspath_prepend` / fixture cleanup to restore `sys.modules` state after the test.

### Nits

#### N1: `redact_args` does not handle nested dicts or lists containing sensitive values

- **File:** `/usr/src/app/core/audit.py:32-47`
- **Dimension:** Correctness
- **Description:** If a tool receives a dict-typed argument containing nested keys like `{"config": {"password": "secret"}}`, the nested `password` would not be redacted because `redact_args` only inspects top-level keys. This is acceptable for the current flat parameter style of MCP tools, but worth noting.
- **Suggested Fix:** No immediate action needed. Document as a known limitation or add recursive handling if tools with nested dict parameters are introduced.

#### N2: Import ordering in `mcp_server.py`

- **File:** `/usr/src/app/mcp_server.py:8-13`
- **Dimension:** Consistency
- **Description:** The `logging` import was moved above the third-party imports, and the `logging.basicConfig(...)` call was moved below the imports from `core.audit`. Previously `logging.basicConfig` was called immediately after the `logging` import (before any other imports). This changes the order in which log configuration is applied -- `core.audit` module-level code (if any) would run before `basicConfig`. Currently `core/audit.py` has no module-level logging calls, so this is harmless, but it is a subtle ordering change.
- **Suggested Fix:** No action required. The current ordering is functionally equivalent given the codebase.

## Remediation Plan

> Ordered fix steps for the coding agent to follow.

### Step 1: Truncate exception messages in audit log

- **File:** `/usr/src/app/core/audit.py`
- **Action:** On line 146, change `error_msg = str(exc)` to `error_msg = str(exc)[:500]` to limit the length of error messages logged, reducing the risk of secrets leaking through exception text.

### Step 2: Add async guard to `audit_wrap`

- **File:** `/usr/src/app/core/audit.py`
- **Action:** After line 126 (`sig = inspect.signature(func)`), add:
  ```python
  if inspect.iscoroutinefunction(func):
      raise TypeError(f"audit_wrap does not support async functions: {func.__name__}")
  ```
  This will produce a clear error if someone tries to wrap an async tool, rather than silently misbehaving.

### Step 3: Fix `test_get_audit_logger_default_path` to avoid real file creation

- **File:** `/usr/src/app/tests/unit/test_audit.py`
- **Action:** Modify the `test_get_audit_logger_default_path` test to use a temporary directory instead of the default path, or add cleanup to remove the handler after the test.

### Step 4: Add cleanup to MCP integration test

- **File:** `/usr/src/app/tests/unit/test_mcp_audit_integration.py`
- **Action:** After the `import mcp_server` and assertions, add `sys.modules.pop("mcp_server", None)` in a `finally` block to restore module state.
