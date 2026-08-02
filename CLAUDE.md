# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

**Hive Mind** is a self-improving personal assistant powered by Claude Code. The system uses a **centralized gateway server** that wraps the Claude CLI's bidirectional stream-json mode, giving every client (Discord, terminal, web) full CLI capabilities through one API.

The nervous system — lucent (vector store + knowledge graph) and comms (the gateway: session manager, broker, HITL) — lives in-repo under [`nervous-system/`](nervous-system/), running as the `hive-lucent` and `hive-comms` containers (compose services `lucent` and `comms`). Everything reaches both over HTTP+bearer; minds hold no lucent or gateway code. The standalone `hive_nervous_system` repo is retired — its code was folded in here.

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
                 │  hive-comms │
                 │   Gateway   │  ← nervous-system/comms/server.py
                 └──────┬──────┘
                        │
              ┌──────────▼──────────┐
              │   Session Manager   │  ← nervous-system/comms/sessions.py
              │   (process pool +   │
              │    SQLite DB)       │
              └──────────┬──────────┘
                         │  mind_id routing
         ┌───────────────┼───────────────┬──────────────┐
  ┌──────▼───────┐ ┌─────▼──────┐ ┌─────▼──────┐ ┌────▼─────────┐
  │ Ada          │ │   Bob      │ │   Bilby    │ │  Nagatha     │
  │ (CLI Claude) │ │(CLI Ollama)│ │ (SDK Code) │ │ (Codex CLI)  │
  └──────┬───────┘ └─────┬──────┘ └─────┬──────┘ └────┬─────────┘
         └───────────────┴───────────────┴──────────────┘
                         │  HTTP + bearer
          ┌──────────────┴──────────────┐
   ┌──────▼──────┐               ┌──────▼──────┐
   │ hive-lucent │               │ hive-tools  │
   │ vector + KG │               │ Gmail/Cal/  │
   │ (shared)    │               │ Docker/HITL │
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
hive-mind/
├── nervous-system/                # Lucent + comms (see nervous-system/README.md)
│   ├── lucent_api/               # Vector store + KG (hive-lucent container)
│   ├── comms/                    # Gateway: sessions, broker, bootstrap, HITL (hive-comms container)
│   ├── tests/                    # Comms test suite (lucent's is lucent_api/tests/)
│   └── data/                     # lucent.db, broker.db, sessions.db (gitignored)
├── config.py                      # Centralized config (loads config.yaml)
├── config.yaml                    # Non-secret settings (providers, models, server)
│
├── core/                          # Internal libraries (not entry points)
│   ├── secrets.py                # Shared get_credential() utility
│   ├── keyring_backend.py        # Keyring backend for containerised minds
│   ├── gateway_client.py         # Shared HTTP client for bots → hive-comms
│   ├── notify_utils.py           # Shared Telegram notification utility
│   ├── path_validation.py        # CWE-22 path traversal protection for skill agents
│   ├── scheduled_skills.py       # Scheduler-driven skill runs
│   ├── skill_telemetry_detect.py # Skill usage telemetry
│   ├── story_pipeline.py         # Post-merge story pipeline (pull, health check, cleanup)
│   └── training_capture*.py      # Per-harness training-turn capture (Claude, Codex)
│
├── tools/
│   └── stateless/                 # Standalone scripts (invoked via skills)
│       ├── crypto/crypto.py      # CoinGecko crypto prices
│       ├── weather/weather.py    # Open-Meteo weather
│       ├── notify/notify.py      # Telegram/email notifications
│       ├── reminders/reminders.py # One-time reminders (SQLite)
│       ├── secrets/secrets.py    # Keyring secret management
│       ├── x_api/x_api.py       # X/Twitter search
│       ├── agent_logs/agent_logs.py # Log file scanner
│       ├── current_time/current_time.py # Timezone-aware clock
│       └── poll_broker/poll_broker.py # Polls broker for inter-mind task results (stdlib only)
│
├── bots/                          # Thin client entry points
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
├── minds/                         # Minds: shared harness code + per-deployment folders
│   ├── harness/                  # Tracked in-container services: claude_cli.py, codex_cli.py
│   ├── proactive.py              # Shared unsolicited-delivery plumbing
│   ├── pty_attach.py             # Shared tmux-backed browser terminal (docs/architecture/browser-terminal.md)
│   ├── example/                  # Tracked starter mind (runtime.yaml + compose fragment)
│   └── <name>/                   # Deployment minds (gitignored): runtime.yaml, prompts, .claude/.codex, container/
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
│   └── claude_code_sdk/          # Vendored Claude Code SDK (legacy/template support)
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
max_sessions: 10
default_model: sonnet

providers:
  anthropic: {}
  ollama:
    env:
      ANTHROPIC_AUTH_TOKEN: "ollama"
      ANTHROPIC_BASE_URL: "http://<ollama-host>:11434"
    api_base: "http://<ollama-host>:11434"

models:
  sonnet: anthropic
  opus: anthropic
  haiku: anthropic
```

Secrets are stored in the system keyring (`keyrings.alt.file.PlaintextKeyring`).
Use `get_credential()` from `core/secrets.py` to read them.

### Which model a session runs on

Three layers, each with one owner:

| Layer | Owner | Lifetime |
|---|---|---|
| `minds/<name>/runtime.yaml` → `default_model` | the mind, on disk | durable truth |
| `broker.minds.model` | comms | a cache of the above |
| `sessions.model` | comms | one conversation's snapshot |

Every mind re-registers from its own `runtime.yaml` on start
(`minds/runtime_api.py` → `POST /broker/minds`, an upsert on `mind_id`), so
the broker row converges on the file rather than drifting from it. The same
module serves `GET`/`PATCH /runtime`, which is how the console edits a mind's
default — the file first, the broker row second, over HTTP for every mind
including the ones on other machines.

`create_session` resolves `caller-supplied model || broker.minds.model`, and
raises when it has neither. Nothing below it defaults: a mind handed a spawn
or an `attach-pty` with no model refuses. A rotation passes the retiring
session's own model explicitly, so a `/model` switch survives it and a
changed default cannot reach into a live conversation — that default is for
the next one.

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
| `POST` | `/broker/messages` | Send inter-mind message (async, returns immediately, wakes callee in background) |
| `GET` | `/broker/messages` | Query messages by `conversation_id` (polling) |
| `GET` | `/broker/conversations/{id}` | Get conversation with all messages |
| `GET` | `/broker/minds` | List all registered minds |
| `POST` | `/broker/minds` | Register a mind |
| `PUT` | `/broker/minds/{name}` | Update mind fields |
| `DELETE` | `/broker/minds/{name}` | Deregister a mind |

## Adding New Tools

Use the `/tool-creator` skill, which reads `specs/tool-migration.md` to determine the right pattern. Preferred is **stateless** — a standalone script wired via a Claude skill:

- Create `tools/stateless/<name>/<name>.py` with argparse + JSON stdout
- Create a Claude skill in `.claude/skills/<name>/SKILL.md` to invoke it
- Editable without any container restart

If a tool genuinely needs a persistent connection (e.g., a long-lived browser session), it can become a small FastAPI service reached over HTTP — same pattern as `hive-lucent` and `hive-tools`.

## Key Design Principles

1. **Claude Code does the heavy lifting** — don't reimplement what it does natively
2. **Tools return raw data** — no LLM formatting layers; the model formats
3. **Self-improvement via tool creation** — new capabilities generated on demand
4. **Less code is better** — if Claude Code already does it, don't wrap it
5. **Gateway is the single source of truth** — all clients go through server.py
6. **Per-process isolation** — env vars set per subprocess, never globally
7. **Always echo directory paths exactly** — whenever a directory path is mentioned (by either party), spell it out character-for-character as you understand it (e.g. `nervous-system`, not "nervous system") so Daniel can catch hyphen/underscore/casing errors before any action is taken.
