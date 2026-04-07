# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

**Hive Mind** is a self-improving personal assistant powered by Claude Code. The system uses a **centralized gateway server** that wraps the Claude CLI's bidirectional stream-json mode, giving every client (Discord, terminal, web) full CLI capabilities through one API.

### Architecture

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│ Discord Bot │  │ Telegram Bot│  │ Group Chat  │  │  Scheduler  │
│  (thin)     │  │  (thin)     │  │  Bot (thin) │  │  (cron)     │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │                 │
       └────────────────┼────────────────┴─────────────────┘
                        │  HTTP / WebSocket
                 ┌──────▼──────┐
                 │   FastAPI   │
                 │   Gateway   │  ← server.py
                 └──────┬──────┘
                        │
              ┌──────────▼──────────┐
              │   Session Manager   │  ← core/sessions.py
              │   (process pool +   │
              │    SQLite DB)       │
              └──────────┬──────────┘
                         │  mind_id routing
         ┌───────────────┼───────────────┬──────────────┐
  ┌──────▼───────┐ ┌─────▼──────┐ ┌─────▼──────┐ ┌────▼─────────┐
  │ Ada          │ │   Bob      │ │   Bilby    │ │  Nagatha     │
  │ (CLI Claude) │ │(CLI Ollama)│ │ (SDK Code) │ │ (SDK Claude) │
  └──────┬───────┘ └─────┬──────┘ └─────┬──────┘ └────┬─────────┘
         └───────────────┴───────────────┴──────────────┘
                         │  MCP (stdio / SSE)
          ┌──────────────┴──────────────┐
   ┌──────▼──────┐               ┌──────▼──────┐
   │ hive-mind-  │               │ hive-mind-  │
   │ tools (int) │               │ mcp (ext +  │
   │             │               │   HITL)     │
   └─────────────┘               └─────────────┘
```

### Self-Improvement

When a user requests something no existing tool handles, Claude Code:
1. Generates the tool code by chaining available terminal tools
2. For requests that are frequent or could benefit from more structure, use the `/tool-creator` skill to create a new tool
3. If an API key is needed, asks the user and uses the `/secrets` skill to store it
4. The new tool is immediately available for use

### Backend Flexibility

The system supports multiple providers configured in `config.yaml`:
- **Anthropic** (default): Full Claude Code capabilities via static aliases (sonnet, opus, haiku)
- **Ollama**: Local/private operation via any Ollama-hosted model (auto-discovered)

Per-subprocess env isolation — no global env mutation.

## Quick Start

```bash
docker compose up -d --build
```

## File Structure

```
hive_mind/
├── server.py                      # FastAPI gateway (HTTP + WebSocket endpoints)
├── mcp_server.py                  # MCP server (FastMCP, direct registration)
├── config.py                      # Centralized config (loads config.yaml)
├── config.yaml                    # Non-secret settings (providers, models, server)
├── .mcp.json                      # Wires MCP tools into Claude Code (host paths)
├── .mcp.container.json            # MCP config for container context
│
├── core/                          # Internal libraries (not entry points)
│   ├── sessions.py               # Session manager (process pool + SQLite)
│   ├── secrets.py                # Shared get_credential() utility
│   ├── models.py                 # Model registry (static aliases + Ollama)
│   ├── gateway_client.py         # Shared HTTP client for bots
│   ├── hitl.py                   # Human-in-the-loop approval
│   ├── audit.py                  # MCP tool invocation audit logging (JSON + rotation)
│   ├── dep_scan.py               # pip-audit wrapper for dependency vulnerability scanning
│   ├── epilogue.py               # Session epilogue processor (post-session memory extraction)
│   ├── kg_guards.py              # Knowledge graph write guards (disambiguation + orphan)
│   ├── memory_expiry.py          # Timed-event expiry sweep for Neo4j
│   ├── memory_schema.py          # Memory data class registry and validation
│   ├── notify_utils.py           # Shared Telegram notification utility
│   ├── path_validation.py        # CWE-22 path traversal protection for skill agents
│   └── story_pipeline.py         # Post-merge story pipeline (pull, health check, cleanup)
│
├── tools/
│   ├── stateful/                  # MCP tools (registered in mcp_server.py)
│   │   ├── browser.py            # Async Playwright browser automation
│   │   ├── knowledge_graph.py    # Neo4j knowledge graph
│   │   ├── memory.py             # Neo4j vector memory store
│   │   ├── group_chat.py         # Group session message forwarding between minds
│   │   └── inter_mind.py         # Direct mind-to-mind delegation
│   │
│   └── stateless/                 # Standalone scripts (invoked via skills)
│       ├── crypto/crypto.py      # CoinGecko crypto prices
│       ├── weather/weather.py    # Open-Meteo weather
│       ├── notify/notify.py      # Telegram/email notifications
│       ├── planka/planka.py      # Planka Kanban board
│       ├── reminders/reminders.py # One-time reminders (SQLite)
│       ├── secrets/secrets.py    # Keyring secret management
│       ├── x_api/x_api.py       # X/Twitter search
│       ├── agent_logs/agent_logs.py # Log file scanner
│       └── current_time/current_time.py # Timezone-aware clock
│
├── clients/                       # Thin client entry points
│   ├── discord_bot.py            # Discord bot
│   ├── telegram_bot.py           # Telegram bot (Ada + named minds)
│   ├── hivemind_bot.py           # Group chat Telegram bot (multi-mind sessions)
│   └── scheduler.py              # Cron daemon
│
├── voice/                         # Voice infrastructure
│   └── voice_server.py           # STT/TTS FastAPI server
│
├── docs/                          # Human-readable documentation and background
├── jobs/                          # Data files (resumes, specs)
├── data/                          # SQLite databases (Docker volume)
│
├── minds/                         # Per-mind backend implementations
│   ├── cli_harness.py            # Shared CLI harness (Ada + Bob)
│   ├── ada/implementation.py     # Ada: cli_claude (Claude CLI)
│   ├── bilby/implementation.py   # Bilby: sdk_code (Claude Code SDK, agentic)
│   ├── bob/implementation.py     # Bob: cli_ollama (Ollama via CLI harness)
│   └── nagatha/implementation.py # Nagatha: sdk_claude (Claude SDK)
│
├── souls/                         # Per-mind identity seed files (one-time use only)
│   ├── ada.md                    # Ada's soul seed
│   ├── bilby.md                  # Bilby's soul seed
│   ├── bob.md                    # Bob's soul seed
│   ├── nagatha.md                # Nagatha's soul seed
│   └── skippy.md                 # Skippy placeholder
│
├── utilities/                     # Standalone utilities (not invoked via skills)
│   └── ollama_tools.py           # Direct Ollama API client
│
├── vendor/                        # Vendored dependencies
│   └── claude_code_sdk/          # Vendored Claude Code SDK (used by Bilby)
│
├── plans/                         # Forward-looking plans and proposals (not yet implemented)
│
├── soul.md                        # Pointer stub (see souls/ada.md)
├── CLAUDE.md                      # This file
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Configuration

Non-secret settings in `config.yaml`:

```yaml
server_port: 8420
idle_timeout_minutes: 30
max_sessions: 10
default_model: sonnet

providers:
  anthropic: {}
  ollama:
    env:
      ANTHROPIC_AUTH_TOKEN: "ollama"
      ANTHROPIC_BASE_URL: "http://203.0.113.10:11434"
    api_base: "http://203.0.113.10:11434"

models:
  sonnet: anthropic
  opus: anthropic
  haiku: anthropic
```

Secrets are stored in the system keyring (`keyrings.alt.file.PlaintextKeyring`).
Use `get_credential()` from `core/secrets.py` to read them.
A minimal `.env` remains for docker-compose interpolation (Neo4j, Planka only).

## Gateway API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/sessions` | Create session |
| `GET` | `/sessions` | List sessions |
| `GET` | `/sessions/{id}` | Get session detail |
| `DELETE` | `/sessions/{id}` | Kill session |
| `POST` | `/sessions/{id}/message` | Send message (SSE streaming) |
| `POST` | `/sessions/{id}/activate` | Activate session on a surface |
| `POST` | `/sessions/{id}/model` | Switch model mid-session |
| `POST` | `/sessions/{id}/autopilot` | Toggle autopilot |
| `WS` | `/sessions/{id}/stream` | WebSocket bidirectional |
| `GET` | `/models` | List available models |
| `POST` | `/command` | Route slash commands |
| `POST` | `/sessions/{id}/remote-control` | Start remote observation of a session |
| `DELETE` | `/sessions/{id}/remote-control` | Stop remote observation |
| `POST` | `/group-sessions` | Create group session (multi-mind) |
| `GET` | `/group-sessions/{id}` | Get group session detail |
| `POST` | `/group-sessions/{id}/message` | Send message to group session |
| `DELETE` | `/group-sessions/{id}` | Kill group session |
| `POST` | `/memory/expiry-sweep` | Trigger timed-event expiry sweep |
| `POST` | `/epilogue/sweep` | Trigger session epilogue sweep |
| `POST` | `/hitl/request` | Submit HITL approval request |
| `GET` | `/hitl/status/{token}` | Check HITL approval status |
| `POST` | `/hitl/respond` | Respond to HITL approval request |

## Adding New Tools

Use the `/tool-creator` skill, which reads `specs/tool-migration.md` to determine the right pattern:

**Stateful tools** (need persistent connections — Neo4j, Playwright, etc.):
- Add functions to `tools/stateful/` and register in `mcp_server.py`
- Available as MCP tools immediately after container restart

**Stateless tools** (standalone scripts — API calls, file ops, etc.):
- Create a script in `tools/stateless/<name>/<name>.py` with argparse + JSON stdout
- Create a Claude skill in `.claude/skills/<name>/SKILL.md` to invoke it
- Editable without any container restart

## Key Design Principles

1. **Claude Code does the heavy lifting** — don't reimplement what it does natively
2. **MCP tools are pure data fetchers** — return raw data, no LLM formatting layers
3. **Self-improvement via tool creation** — new capabilities generated on demand
4. **Less code is better** — if Claude Code already does it, don't wrap it
5. **Gateway is the single source of truth** — all clients go through server.py
6. **Per-process isolation** — env vars set per subprocess, never globally
7. **Always echo directory paths exactly** — whenever a directory path is mentioned (by either party), spell it out character-for-character as you understand it (e.g. `hive_mind_mcp`, not "hive mind mcp") so Daniel can catch hyphen/underscore/casing errors before any action is taken.
