# [DevOps] Expand Audit Logging to All MCP Tool Invocations

**Card ID:** 1720154169131664449
**Type:** story
**List:** In Progress

## Problem

Currently only `create_tool` and `install_dependency` are audit-logged (in `agents/tool_creator.py`). All other MCP tool calls are invisible from an audit perspective — there's no record of which tools were called, with what arguments, or what they returned.

## Scope

- Log all tool calls: name, caller (if determinable), args summary (no secrets), timestamp, result status
- Rotate logs to prevent unbounded growth
- Structured JSON logging for easier analysis

## Acceptance Criteria

- [ ] All MCP tool invocations are logged with: tool name, timestamp, args summary (secrets redacted), result status (success/error)
- [ ] Log is structured JSON (one entry per line / NDJSON)
- [ ] Log rotation in place (max file size or daily rotation)
- [ ] No secrets appear in the audit log
- [ ] Existing audit logging in tool_creator.py is preserved and consistent with new format

## Architecture Context

- MCP server is `mcp_server.py` — it discovers and serves tools from `agents/`
- Tool execution flows through `agent_tooling` library
- Audit log currently at `audit.log` in project root (from `tool_creator.py`)
- The `agent_tooling` library likely has a registration mechanism we can hook into

## Files Likely Involved

- `mcp_server.py` — MCP server entry point
- `agents/tool_creator.py` — existing audit logging to model from
- `agents/agent_logs.py` — existing agent_logs tool (may be relevant)
- Potentially a new `core/audit.py` or additions to existing modules
