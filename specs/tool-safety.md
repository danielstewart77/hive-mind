# Tool Safety — AST Validation & Process Isolation

## Ring 1: AST Validation

Before any runtime-created tool is loaded, its source code is parsed with Python's `ast` module and checked against a blocklist of dangerous patterns.

### Blocked Patterns
- `eval`, `exec`, `compile`, `__import__`, `breakpoint`
- `os.system`, `subprocess` with `shell=True`
- Imports of: `pty`, `ctypes`, `socket`, `multiprocessing`, `code`, `codeop`

### Staging Flow
1. Code is written to `agents/staging/` first
2. AST validation runs against the blocklist
3. If clean, promoted to `agents/`
4. If violations found, rejected with audit logging

### First-Party vs Dynamic Tools
- **First-party tools** (hand-written, committed to repo) run in-process — no AST check
- **Dynamic tools** (created via `create_tool`) go through full AST validation + staging

## Ring 2: Process Isolation

Dynamically created MCP tools run in child subprocesses with a stripped environment.

### Subprocess Environment
The child process receives only 5 base env vars:
- `PATH`, `PYTHONPATH`, `HOME`, `VIRTUAL_ENV`, `LANG`
- Plus any explicitly declared via `allowed_env` parameter on `create_tool`

### Timeout
30-second timeout kills runaway tools.

### Implementation
`core/tool_runner.py` handles subprocess execution and env stripping.

## Rules for New Tools

- All `@tool()` decorated functions in `agents/` are auto-discovered by the MCP server
- Return raw data (JSON strings preferred) — never format for display
- Read credentials via `get_credential(key)` — never hardcode
- No module-level side effects (no DB connections at import time)
- Catch specific exceptions; return `{"error": "brief description"}` on failure
- All `subprocess.run` calls must use list arguments (`shell=False`)
