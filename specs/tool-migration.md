# Tool Migration Proposal: agent_tooling to Hybrid Pattern

## Problem

All MCP tools currently live in `agents/` using the `agent_tooling` library's `@tool()` decorator. This creates several pain points:

1. **Code changes require MCP container restart** — Python caches imported modules, so editing a tool file has no effect until `hive_mind_mcp` restarts.
2. **Dependency conflicts** — All tools share one Python environment. The Playwright sync/async conflict is a direct example.
3. **`agent_tooling` friction** — No async support, force-reinstall required in Dockerfile, adds a third-party dependency for what amounts to function registration.
4. **No process isolation** — A crashing tool can take down the entire MCP server.

## Proposed Architecture

Split tools into two categories based on a single criterion: **does the tool need to persist state between calls?**

```
tools/
├── stateless/                     # Standalone scripts, own venvs
│   ├── weather/
│   │   ├── weather.py
│   │   ├── requirements.txt
│   │   └── venv/
│   ├── crypto/
│   │   ├── crypto.py
│   │   ├── requirements.txt
│   │   └── venv/
│   └── ...
│
└── stateful/                      # Loaded by MCP server (no @tool decorator)
    ├── browser.py
    ├── knowledge_graph.py
    └── memory.py
```

### Stateless Tools (standalone scripts)

Each tool is a self-contained Python script in its own directory with its own venv. Claude invokes them via Bash, guided by a corresponding Claude skill.

**Invocation pattern:**
```bash
/usr/src/app/tools/stateless/weather/venv/bin/python \
  /usr/src/app/tools/stateless/weather/weather.py \
  --location "missouri city, tx" --time-span "today"
```

**Script contract:**
- Accept arguments via `argparse` (or simple `sys.argv`)
- Print JSON to stdout
- Exit 0 on success, non-zero on error
- No module-level state required

**Claude skill contract:**
- Skill SKILL.md documents the tool's purpose, arguments, and invocation command
- Claude reads the skill, constructs the bash call, parses stdout JSON

### Stateful Tools (MCP server)

A small number of tools that maintain persistent connections (Neo4j drivers, browser sessions) remain in the MCP server. The MCP server loads them directly — no `agent_tooling` library, just plain `FastMCP` registration.

**MCP server loads from `tools/stateful/`** instead of `agents/`.

## Tool Classification

### Stateless (move to `tools/stateless/`)

| Tool | Current File | Dependencies | Notes |
|------|-------------|-------------|-------|
| Weather | `get_weather_for_location.py` | `requests` | Pure HTTP |
| Crypto Price | `coingecko.py` | `requests` | Pure HTTP |
| Current Time | `get_current_time.py` | (stdlib) | No deps at all |
| Notifications | `notify.py` | `httpx` | HTTP calls, no persistent state |
| Planka | `planka.py` | `requests` | Gets fresh auth token per call |
| Reminders | `reminders.py` | `dateparser` | SQLite, but opens/closes per call |
| Secrets | `secret_manager.py` | `keyring` | Keyring access, no persistent state |
| X/Twitter | `x_api.py` | `requests` | Pure HTTP |
| Agent Logs | `agent_logs.py` | (stdlib) | File-based position tracking |
| ~~Code Genius~~ | `skill_code_genius.py` | (subprocess) | **Delete, not migrate.** Redundant — actual Claude skills (`/code-genius`, `/code-review-genius`, `/planning-genius`) already exist. |
| ~~Code Review~~ | `skill_code_review_genius.py` | (subprocess) | See above |
| ~~Planning Genius~~ | `skill_planning_genius.py` | (subprocess) | See above |

### Stateful (move to `tools/stateful/`)

| Tool | Current File | State | Why |
|------|-------------|-------|-----|
| Browser | `browser.py` | Browser session dict, Playwright thread pool | Playwright needs a running browser process |
| Knowledge Graph | `knowledge_graph.py` | Neo4j driver singleton | Connection pooling, index creation guard |
| Memory (vectors) | `memory.py` | Neo4j driver singleton | Connection pooling, index creation guard |

### Special Case: `tool_creator.py` → `/tool-creator` skill

The current `tool_creator.py` wraps what Claude already does natively (writing code) in an AST validator and file writer. Replace it entirely with a redesigned `/tool-creator` skill that:

1. Reads this migration spec to determine if the new tool is stateful or stateless
2. For **stateless**: creates the script + `requirements.txt` + venv + Claude skill via `/skill-creator-claude`
3. For **stateful**: adds the function to the appropriate file in `tools/stateful/` and registers it in `mcp_server.py` (rare — requires MCP restart)
4. References `specs/security/tool-creation-rules.md` for security constraints (replaces the Python AST validator with spec-driven reasoning)

This follows the architecture principle: logic that requires interpretation lives in a spec/skill, not in Python code.

### Special Case: `secret_manager.py`

Many tools import `get_credential()` from this module. Two options:
1. **Shared library** — Keep `get_credential()` as a utility function in `core/` that other tools can import (stateful tools via Python import, stateless tools via a small helper)
2. **Subprocess call** — Stateless tools call the secret manager script to get credentials before making their API calls

Recommendation: Move `get_credential()` to `core/secrets.py` as a shared utility. Stateless tools that need secrets include a small bootstrap that imports from `core/`.

## Migration Steps

Safety strategy: **duplicate, don't delete.** Every phase copies files to the new location while leaving originals in `agents/` untouched. The old MCP tools remain functional as fallback throughout. Only the final cleanup phase removes anything.

### Phase 0: Validate FastMCP Direct Registration (in-place, no file moves)

Test that we can remove `agent_tooling` from the MCP server without breaking anything.

1. In `mcp_server.py`, replace `agent_tooling` auto-discovery with direct `FastMCP` registration for **browser.py only**:
   - Remove `@tool()` decorator from browser functions
   - Import and register them directly with `mcp.tool()`
   - All other tools continue using `agent_tooling` during transition
2. Rebuild `hive_mind_mcp`, test all browser tools work
3. Convert browser.py to async Playwright API (now possible since FastMCP supports async natively)
4. Rebuild, test again — confirm the sync/async issue is resolved

**Rollback:** Revert `mcp_server.py` and `browser.py` changes. Zero risk.

### Phase 1: Scaffold + Duplicate Stateless Tools

Create the new directory structure and copy (not move) all tools.

1. Create `tools/stateless/` and `tools/stateful/` directories
2. **Copy** all stateless tools to `tools/stateless/<tool-name>/`:
   - Port each to a standalone script with argparse + JSON stdout
   - Create `requirements.txt` with tool-specific dependencies
   - Create and populate venv
   - Create Claude skill via `/skill-creator-claude` with invocation instructions
3. **Copy** all stateful tools to `tools/stateful/`:
   - Remove `@tool()` decorators in the copies
   - These are dormant — `mcp_server.py` still loads from `agents/`
4. `agents/` is completely untouched — all original MCP tools still work

**No conflicts:** Stateless tools in `tools/` are invoked via Bash (skill-guided). MCP tools in `agents/` are invoked via `mcp__hive-mind-tools__<name>`. Different namespaces, both can coexist.

**Duplication order** (simplest first):
1. `get_current_time.py` (no deps)
2. `coingecko.py` (one HTTP call)
3. `get_weather_for_location.py` (one HTTP call)
4. `x_api.py` (HTTP + auth)
5. `notify.py` (HTTP + multiple channels)
6. `planka.py` (HTTP + auth, many functions — one script with subcommands)
7. `reminders.py` (SQLite + dateparser)
8. `agent_logs.py` (file I/O)
9. `secret_manager.py` (keyring — also copy `get_credential()` to `core/secrets.py`)
10. `skill_*.py` files — **skip, delete in Phase 4.** Redundant with existing Claude skills.

**Rollback:** Delete `tools/` directory. Zero risk.

### Phase 2: Test New Versions

Test each stateless tool via its skill, verifying it produces the same output as the MCP version.

1. For each tool, explicitly invoke the skill-based version
2. Compare output with the MCP tool version
3. Verify tool edits take effect without any restart
4. Mark each tool as validated

**Rollback:** Stop using skills, fall back to MCP tools. Zero risk.

### Phase 3: Cut Over MCP Server

Once all tools are validated, switch the MCP server to the new structure.

1. Update `mcp_server.py` to load from `tools/stateful/` using direct `FastMCP` registration — no `agent_tooling`
2. Remove `agent_tooling` from `requirements.txt` and Dockerfile
3. Rebuild `hive_mind_mcp`
4. Verify all stateful MCP tools work (browser, knowledge graph, memory)
5. Verify stateless tools still work via skills (unaffected by this change)

**Rollback:** Revert `mcp_server.py` and `requirements.txt`, rebuild. `agents/` still has all the originals.

### Phase 4: Cleanup (only after full validation)

1. Remove `agents/` directory
2. Update `CLAUDE.md` with new tool creation instructions
3. Replace `mcp-tool-builder` skill with new `/tool-creator` skill
4. Update architecture docs

**No rollback needed** — everything has been validated in prior phases.

## Venv Management

Each stateless tool gets its own venv to avoid dependency conflicts. Venvs are created at build time (Dockerfile) or on-demand by the tool creator.

**Dockerfile addition:**
```dockerfile
# Create venvs for stateless tools
COPY tools/stateless/ /usr/src/app/tools/stateless/
RUN for dir in /usr/src/app/tools/stateless/*/; do \
      if [ -f "$dir/requirements.txt" ]; then \
        python3 -m venv "$dir/venv" && \
        "$dir/venv/bin/pip" install --no-cache-dir -r "$dir/requirements.txt"; \
      fi; \
    done
```

**On-demand creation** (for dynamically created tools):
```bash
python3 -m venv tools/stateless/<tool>/venv
tools/stateless/<tool>/venv/bin/pip install -r tools/stateless/<tool>/requirements.txt
```

## Skill Template

Each stateless tool gets a Claude skill. Example for weather:

```markdown
---
name: weather
description: Get weather for a location
user-invocable: false
---

# Weather Tool

Get current or forecasted weather for any location.

## Usage

Run the weather tool via Bash:

\```bash
/usr/src/app/tools/stateless/weather/venv/bin/python \
  /usr/src/app/tools/stateless/weather/weather.py \
  --location "<city, state>" \
  --time-span "<today|tomorrow|week>"
\```

## Arguments

- `--location` (required): City and state/country (e.g., "missouri city, tx")
- `--time-span` (optional): "today" (default), "tomorrow", or "week"

## Output

JSON object with temperature, conditions, and forecast data.
```

## Benefits

- **True dynamism** — Edit a tool script, next call picks it up. No restart.
- **Process isolation** — A tool crash can't take down the MCP server.
- **Independent dependencies** — Each tool has exactly the packages it needs.
- **Simpler MCP server** — Only 3 tools to load, no agent_tooling dependency.
- **Better tool creation** — New tools are just a script + skill, no framework knowledge needed.

## Risks

- **Two patterns** — Developers need to know which pattern applies (mitigated: the boundary is clear and documented)
- **Process startup cost** — Each stateless tool call launches a Python process (mitigated: most calls are sub-second; heavy tools like Playwright stay in MCP)
- **Shared utilities** — Tools that import from `core/` need access to those modules (mitigated: `PYTHONPATH` or `sys.path` in scripts)
- **Secret access** — Stateless tools need credentials (mitigated: `core/secrets.py` shared utility)
