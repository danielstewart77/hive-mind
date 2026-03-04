# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

**Hive Mind** is a self-improving personal assistant powered by Claude Code. The system uses a **centralized gateway server** that wraps the Claude CLI's bidirectional stream-json mode, giving every client (Discord, terminal, web) full CLI capabilities through one API.

### Architecture

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│ Discord Bot │  │ Terminal UI │  │   Web UI    │
│  (thin)     │  │  (thin)     │  │  (thin)     │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       └────────────────┼────────────────┘
                        │  HTTP / WebSocket
                 ┌──────▼──────┐
                 │   FastAPI   │
                 │   Gateway   │  ← server.py
                 └──────┬──────┘
                        │
              ┌─────────▼─────────┐
              │  Session Manager  │  ← core/sessions.py
              │  (process pool +  │
              │   SQLite DB)      │
              └─────────┬─────────┘
                        │  stdin/stdout (NDJSON)
              ┌─────────▼─────────┐
              │  claude -p        │
              │  --stream-json    │
              │  + MCP tools      │
              │  (one per session)│
              └───────────────────┘
```

### Self-Improvement

When a user requests something no existing tool handles, Claude Code:
1. Generates the tool code
2. Calls `create_tool` MCP tool to write it to `agents/` and register it
3. If an API key is needed, asks the user and calls `set_secret` to store it
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
├── mcp_server.py                  # MCP server exposing agent tools (stdio)
├── config.py                      # Centralized config (loads config.yaml)
├── config.yaml                    # Non-secret settings (providers, models, server)
├── .mcp.json                      # Wires MCP tools into Claude Code (host paths)
├── .mcp.container.json            # MCP config for container context
│
├── core/                          # Internal libraries (not entry points)
│   ├── sessions.py               # Session manager (process pool + SQLite)
│   ├── models.py                 # Model registry (static aliases + Ollama)
│   ├── gateway_client.py         # Shared HTTP client for bots
│   └── hitl.py                   # Human-in-the-loop approval
│
├── clients/                       # Thin client entry points
│   ├── discord_bot.py            # Discord bot
│   ├── telegram_bot.py           # Telegram bot
│   └── scheduler.py              # Cron daemon
│
├── voice/                         # Voice infrastructure
│   └── voice_server.py           # STT/TTS FastAPI server
│
├── agents/                        # MCP tools (@tool decorated, auto-discovered)
│   ├── secret_manager.py         # Keyring secret management
│   ├── knowledge_graph.py        # Neo4j knowledge graph
│   ├── planka.py                 # Planka Kanban board
│   ├── notify.py                 # Telegram/email notifications
│   ├── coingecko.py              # Crypto prices
│   ├── tool_creator.py           # Runtime tool creation
│   └── [dynamically created tools]
│
├── documents/                     # Plans, specs, reviews
├── jobs/                          # Data files (resumes, specs)
├── data/                          # SQLite databases (Docker volume)
├── backups/                       # Manual backups
│
├── soul.md                        # Ada's identity (fallback stub)
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
Use `get_credential()` from `agents/secret_manager.py` to read them.
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

## Adding New MCP Tools

Create a Python file in `agents/` with the `@tool()` decorator:

```python
from agent_tooling import tool

@tool(tags=["example"])
def my_tool(param: str) -> str:
    """Clear description of what this tool does."""
    return result
```

The tool is auto-discovered by the MCP server. Return raw data (JSON strings preferred) — Claude Code handles formatting for the user.

## Key Design Principles

1. **Claude Code does the heavy lifting** — don't reimplement what it does natively
2. **MCP tools are pure data fetchers** — return raw data, no LLM formatting layers
3. **Self-improvement via tool creation** — new capabilities generated on demand
4. **Less code is better** — if Claude Code already does it, don't wrap it
5. **Gateway is the single source of truth** — all clients go through server.py
6. **Per-process isolation** — env vars set per subprocess, never globally
