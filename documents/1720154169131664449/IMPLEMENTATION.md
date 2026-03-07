# Implementation Plan: Story #1720154169131664449 - Expand Audit Logging to All MCP Tool Invocations

## Overview

Add structured JSON audit logging to every MCP tool invocation. Currently only `create_tool` and `install_dependency` have audit logs. This creates a centralised `core/audit.py` module with secret redaction and log rotation, wraps all tool functions in `mcp_server.py` with audit logging, and migrates `tool_creator.py` to use the shared audit module.

## Technical Approach

- **Interception point:** `mcp_server.py` — wrap each tool function with an audit decorator before registering it with FastMCP. This is the single funnel all MCP tool calls pass through.
- **Centralised module:** `core/audit.py` — provides `get_audit_logger()` (configured with `RotatingFileHandler` and JSON formatter) and `audit_wrap(func)` (decorator that logs tool name, args summary, result status, duration).
- **Secret redaction:** A static set of sensitive parameter name patterns (`value`, `password`, `token`, `secret`, `auth`, `key` when it's the only param meaning "secret key") plus truncation for very long string args (like `code`). Applied at log time, never mutates actual args.
- **Existing logging preserved:** `tool_creator.py`'s detailed security audit logs (TOOL_CREATE, TOOL_REJECTED, etc.) remain as-is — they log code content and security violations which the general audit layer does not. The general layer catches the outer call; the inner layer catches security-specific events.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|------------|-------|
| Audit logging setup | `agents/tool_creator.py:28-33` | `logging.getLogger("hive_mind.audit")` + FileHandler |
| Tool wrapping in MCP | `mcp_server.py:21-26` | `for schema in get_tool_schemas(): mcp.tool()(func)` |
| JSON structured return | `agents/agent_logs.py` | Returns `json.dumps({...})` pattern |
| RotatingFileHandler | Python stdlib `logging.handlers` | Standard log rotation |

## Models & Schemas

No new Pydantic models needed. The audit log entries are plain JSON dicts written via the logging module:

```python
{
    "timestamp": "2026-03-02T03:00:00.123Z",
    "event": "tool_call",
    "tool": "memory_store",
    "args": {"content": "test memory", "tags": "session", "value": "***REDACTED***"},
    "status": "success",  # or "error"
    "duration_ms": 42,
    "error": null  # or brief error message
}
```

## Implementation Steps

### Step 1: Create `core/audit.py` — centralised audit logger with JSON formatting, redaction, rotation

**Files:**
- Create: `core/audit.py` — audit logger factory, JSON formatter, secret redaction, tool wrapper

**Test First (unit):** `tests/unit/test_audit.py`
- [ ] `test_redact_secrets_redacts_value_param` — asserts args dict with key "value" gets "***REDACTED***"
- [ ] `test_redact_secrets_redacts_password_param` — asserts "password" key gets redacted
- [ ] `test_redact_secrets_truncates_long_code` — asserts "code" param > 200 chars is truncated to "[200 chars]"
- [ ] `test_redact_secrets_preserves_safe_params` — asserts normal params like "query", "board_id" are untouched
- [ ] `test_json_formatter_produces_valid_json` — asserts log record is valid JSON with required fields
- [ ] `test_audit_wrap_logs_success` — asserts successful tool call produces a log entry with status="success"
- [ ] `test_audit_wrap_logs_error` — asserts tool that raises exception produces status="error" and error message
- [ ] `test_audit_wrap_preserves_return_value` — asserts wrapped function returns same value as original
- [ ] `test_audit_wrap_preserves_exception` — asserts wrapped function re-raises the original exception

**Then Implement:**
- [ ] Create `core/audit.py` with:
  - `SENSITIVE_PARAMS`: frozenset of param name patterns to redact (`"value"`, `"password"`, `"token"`, `"secret"`, `"auth"`)
  - `MAX_ARG_LENGTH = 200`: truncation threshold for long string args
  - `redact_args(args: dict) -> dict`: returns a copy with sensitive values replaced by `"***REDACTED***"` and long strings truncated
  - `class JSONAuditFormatter(logging.Formatter)`: formats log records as JSON with timestamp, event, tool, args, status, duration_ms, error
  - `get_audit_logger(log_path, max_bytes, backup_count) -> logging.Logger`: returns a logger named `"hive_mind.audit"` with a `RotatingFileHandler` (default 5MB max, 3 backups) and the JSON formatter
  - `audit_wrap(func, logger) -> callable`: decorator that logs before/after each call with redacted args and timing

**Verify:** `pytest tests/unit/test_audit.py -v`

---

### Step 2: Integrate audit wrapping into `mcp_server.py`

**Files:**
- Modify: `mcp_server.py` — import audit module, create logger, wrap each tool function before registering with FastMCP

**Test First (unit):** `tests/unit/test_mcp_audit_integration.py`
- [ ] `test_mcp_wraps_tools_with_audit` — asserts that after tool registration, calling a tool produces an audit log entry (mock the logger)

**Then Implement:**
- [ ] In `mcp_server.py`, after `discover_tools(["agents"])`:
  - Import `get_audit_logger` and `audit_wrap` from `core.audit`
  - Create audit logger: `audit_logger = get_audit_logger("/usr/src/app/audit.log")`
  - In the registration loop, wrap each func: `func = audit_wrap(func, audit_logger)` before `mcp.tool()(func)`
- [ ] Preserve the existing `log.info(f"[MCP] {name}")` registration log

**Verify:** `pytest tests/unit/test_mcp_audit_integration.py -v`

---

### Step 3: Migrate `tool_creator.py` to use shared audit logger

**Files:**
- Modify: `agents/tool_creator.py` — replace manual logger setup with `get_audit_logger()` from `core.audit`

**Test First (unit):** `tests/unit/test_tool_creator_audit.py`
- [ ] `test_tool_creator_uses_shared_logger` — asserts `_audit` logger in tool_creator uses RotatingFileHandler (not plain FileHandler)

**Then Implement:**
- [ ] Replace lines 28-33 in `tool_creator.py` (manual logger setup) with:
  ```python
  from core.audit import get_audit_logger
  _audit = get_audit_logger()
  ```
- [ ] Remove the manual FileHandler setup
- [ ] Keep all existing `_audit.info(...)` and `_audit.warning(...)` calls unchanged — they log security-specific detail that the general wrapper doesn't capture

**Verify:** `pytest tests/unit/test_tool_creator_audit.py -v`

---

## Integration Checklist

- [ ] No new routes needed in `server.py`
- [ ] No new MCP tools needed (this is infrastructure, not a tool)
- [ ] No config additions needed (log path and rotation params are hardcoded with sensible defaults)
- [ ] No new dependencies needed (uses stdlib `logging.handlers.RotatingFileHandler`)
- [ ] No secrets involved

## Build Verification

- [ ] `pytest tests/ -v` passes
- [ ] `mypy core/audit.py --ignore-missing-imports` passes
- [ ] `ruff check core/audit.py mcp_server.py agents/tool_creator.py` passes
- [ ] All 5 ACs addressed:
  1. All MCP tool invocations logged ✓ (Step 2)
  2. Structured JSON format ✓ (Step 1 — JSONAuditFormatter)
  3. Log rotation ✓ (Step 1 — RotatingFileHandler)
  4. No secrets in logs ✓ (Step 1 — redact_args)
  5. Existing logging preserved ✓ (Step 3 — tool_creator keeps security logs)
