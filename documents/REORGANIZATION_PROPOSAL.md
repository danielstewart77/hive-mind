# Project Reorganization Proposal

**Date:** 2026-02-28
**Status:** Draft — awaiting Daniel's review

## Problem

The root directory has 15+ Python files mixed together: entry-point services, shared libraries, infrastructure utilities, and config files. It works, but navigating is cluttered — especially as the project grows.

## Current Root (Python files only)

```
/usr/src/app/
├── server.py           ← FastAPI gateway (entry point)
├── discord_bot.py      ← Discord bot (entry point)
├── telegram_bot.py     ← Telegram bot (entry point)
├── scheduler.py        ← Cron daemon (entry point)
├── voice_server.py     ← STT/TTS server (entry point)
├── mcp_server.py       ← MCP stdio server (entry point)
├── sessions.py         ← Session manager (core lib)
├── models.py           ← Model registry (core lib)
├── config.py           ← Config loader (core lib)
├── gateway_client.py   ← Bot HTTP client (shared util)
├── hitl.py             ← Human-in-the-loop (shared util)
├── audit.log           ← Audit trail (generated data)
├── __init__.py         ← Package marker
├── agents/             ← MCP tools (17 files)
├── skills/             ← STALE: duplicate of ~/.claude/skills/ (delete)
├── documents/          ← Plans, specs, reviews
├── jobs/               ← Data files (resumes, specs)
├── data/               ← SQLite databases (Docker volume)
└── shared/             ← Reserved (empty)
```

## Proposed Structure

```
/usr/src/app/
├── server.py                  # Gateway — stays at root (main entry point)
├── mcp_server.py              # MCP server — stays at root (Claude CLI spawns it)
├── config.py                  # Config — stays at root (imported everywhere)
├── config.yaml                # Settings file
│
├── core/                      # Internal libraries (not entry points)
│   ├── __init__.py
│   ├── sessions.py            # Session manager
│   ├── models.py              # Model registry
│   ├── gateway_client.py      # Shared HTTP client for bots
│   └── hitl.py                # Human-in-the-loop approval
│
├── clients/                   # Thin client entry points
│   ├── __init__.py
│   ├── discord_bot.py         # Discord bot
│   ├── telegram_bot.py        # Telegram bot
│   └── scheduler.py           # Cron daemon
│
├── voice/                     # Voice infrastructure
│   ├── __init__.py
│   └── voice_server.py        # STT/TTS FastAPI server
│
├── agents/                    # MCP tools (unchanged)
│   ├── coingecko.py
│   ├── planka.py
│   ├── ...
│   └── knowledge_graph.py
│
├── documents/                 # Plans, specs (unchanged)
├── jobs/                      # Data files (unchanged)
├── data/                      # SQLite DBs (Docker volume, unchanged)
│
├── CLAUDE.md
├── soul.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .mcp.json / .mcp.container.json
```

## What Moves Where

| File | From | To | Rationale |
|------|------|----|-----------|
| `sessions.py` | root | `core/` | Library, not an entry point |
| `models.py` | root | `core/` | Library, not an entry point |
| `gateway_client.py` | root | `core/` | Shared utility for all bots |
| `hitl.py` | root | `core/` | Infrastructure utility |
| `discord_bot.py` | root | `clients/` | Thin client entry point |
| `telegram_bot.py` | root | `clients/` | Thin client entry point |
| `scheduler.py` | root | `clients/` | Thin client entry point |
| `voice_server.py` | root | `voice/` | Standalone service, distinct concern |

## What Stays at Root

| File | Why |
|------|-----|
| `server.py` | Main gateway — the heart of the system |
| `mcp_server.py` | Claude CLI spawns it via `.mcp.json` path; moving it complicates MCP config |
| `config.py` | Imported by everything; root keeps imports simple |

## What Gets Deleted

| Item | Reason |
|------|--------|
| `skills/` | Stale duplicate. All 7 skills here are older copies of what lives in `~/.claude/skills/` (18 skills). Claude Code discovers skills from `~/.claude/skills/`, not from this folder. It's dead weight. |
| `shared/` | Empty, unused — `core/` replaces its intended purpose |
| `__init__.py` (root) | Not needed if we're not importing the root as a package |

### Note on `.claude/` host mount

The `~/.claude` directory is bind-mounted from the host into the container. This mount **should stay** — it contains credentials, session history, settings, hooks, memory files, and the actual skills directory that Claude Code uses. A named Docker volume would work functionally, but the bind mount gives Daniel visibility and editability from the host (markdown editor, file browser). The project `skills/` folder was likely created before this mount existed and is now redundant.

## Files That Need Updating

### Import Changes

**server.py:**
```python
# Before
from sessions import SessionManager
from models import ModelRegistry, Provider
from hitl import hitl_store, TOKEN_TTL

# After
from core.sessions import SessionManager
from core.models import ModelRegistry, Provider
from core.hitl import hitl_store, TOKEN_TTL
```

**clients/discord_bot.py:**
```python
# Before
from gateway_client import GatewayClient, get_skills, time_ago

# After
from core.gateway_client import GatewayClient, get_skills, time_ago
```

**clients/telegram_bot.py:** Same pattern as discord_bot.py

**clients/scheduler.py:** Same pattern

**core/sessions.py:**
```python
# Before
from models import ModelRegistry, Provider

# After
from core.models import ModelRegistry, Provider
```

### docker-compose.yml

```yaml
# Before
command: python discord_bot.py
command: python telegram_bot.py
command: python scheduler.py
command: python voice_server.py

# After
command: python -m clients.discord_bot
command: python -m clients.telegram_bot
command: python -m clients.scheduler
command: python -m voice.voice_server
```

### .mcp.json / .mcp.container.json

No change needed — `mcp_server.py` stays at root.

## Agents — No Changes

The `agents/` directory is already well-organized. Tools are auto-discovered by `mcp_server.py`. Moving them would break the discovery mechanism for no benefit. Skills live in `~/.claude/skills/` (host-mounted), not in the project repo.

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Import paths break | Medium | Straightforward find-and-replace; test each service after |
| Docker entry points break | Medium | Update `command:` in docker-compose.yml, add `__main__.py` or use `-m` |
| MCP server path breaks | Low | Not moving it — stays at root |
| Running services during migration | Low | Do it in one commit, rebuild all containers |

## Migration Steps

1. Create `core/`, `clients/`, `voice/` directories with `__init__.py`
2. Move files to their new locations
3. Update all imports (find-and-replace)
4. Update `docker-compose.yml` entry points
5. Delete `shared/` and root `__init__.py`
6. Update `CLAUDE.md` file structure section
7. `docker compose up -d --build` to rebuild everything
8. Smoke test each service

## Decision Points for Daniel

1. **Do you want `server.py` at root or in `core/`?** I kept it at root because it's THE main process, but it could go in `core/` too.
2. **`voice/` as its own directory or merge into `core/`?** It's a standalone FastAPI server with its own concern (audio processing). Feels like it deserves its own directory.
3. **Rename `clients/` to `bots/`?** Scheduler isn't really a "bot" — it's a cron daemon. "Clients" is more accurate since they're all HTTP clients to the gateway.
4. **Move `config.py` into `core/`?** I left it at root for simplicity, but it logically belongs with the other infrastructure code.
