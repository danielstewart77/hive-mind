---
name: tool-creator
description: "Create a new Hive Mind tool. Reads specs/tool-migration.md to determine stateless vs stateful, then scaffolds the tool, skill, and tests."
argument-hint: [tool-description]
tools: Read, Write, Edit, Glob, Grep, Bash, Skill
model: opus
user-invocable: true
---

# Tool Creator

You create new tools for the Hive Mind system. Read `specs/tool-migration.md` first to determine whether the tool should be **stateless** or **stateful**.

## Decision

- **Stateful** = needs persistent connections (database drivers, browser sessions, long-lived sockets). Goes in `tools/stateful/`, registered in `mcp_server.py`.
- **Stateless** = everything else. Goes in `tools/stateless/<name>/`, invoked via a Claude skill.

## Stateless Tool Workflow

1. Create `tools/stateless/<name>/<name>.py` with argparse + JSON stdout
2. Create `tools/stateless/<name>/requirements.txt`
3. Create skill at `.claude/skills/<name>/SKILL.md` (use `/skill-creator-claude` for format)
4. Copy skill to `specs/skills/<name>/SKILL.md`
5. Create the per-tool venv: `python3 -m venv tools/stateless/<name>/venv && tools/stateless/<name>/venv/bin/pip install -r tools/stateless/<name>/requirements.txt`
6. Use `tools/stateless/<name>/venv/bin/python` in skill invocation commands
7. Handle secrets via `core.secrets.get_credential()` (add `sys.path` to import). **If the tool reads any secrets, add `keyring` and `keyrings.alt` to `requirements.txt`** — the per-tool venv doesn't inherit these from the main container venv.
8. Test: `tools/stateless/<name>/venv/bin/python tools/stateless/<name>/<name>.py --help`

## Stateful Tool Workflow

1. Add functions to `tools/stateful/<module>.py`
2. Export via module-level list (e.g. `MY_TOOLS = [func1, func2]`)
3. Register in `mcp_server.py`: import list, add to registration loop
4. Use `core.secrets.get_credential()` for secrets
5. MCP container restart required

## Code Standards

- Return JSON strings for structured data
- Handle errors gracefully (return error JSON, don't raise)
- Never hardcode secrets
- Add deps to `requirements.txt` under appropriate section
