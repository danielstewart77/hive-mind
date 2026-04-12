---
name: setup-config
description: Generate configuration files for a new Hive Mind deployment. Creates config.yaml, .env, and .mcp.json from user inputs. Detects port conflicts and configures volume paths.
argument-hint: "[--import <path>]"
---

# setup-config

Generate all configuration files for a Hive Mind deployment from user inputs.

## Step 1 — Check for existing config or import

Check if `config.yaml` already exists:
- If yes, ask: "Config exists. Overwrite, edit, or skip?"
- Check for `$ARGUMENTS` containing `--import <path>`. If an import bundle is provided, load it and pre-populate all values from the export.

## Step 2 — Gather inputs

Ask the user for:
- **Server port** (default 8420). Check if port is in use: `ss -tlnp | grep :<port>`. If conflict, suggest next available port.
- **Installation directory** — where the project root lives (default: current directory)
- **Data volume paths:**
  - Neo4j data directory (default: Docker named volume)
  - Model storage directory (default: Docker named volume)
  - Mind data directory (default: Docker named volume)
  - Or: "Use default Docker volumes for everything" (simplest)
- **Compose profile** — from `/setup-prerequisites` recommendation, or ask: gpu-nvidia, gpu-amd, cpu-only, minimal

## Step 3 — Generate config.yaml

Write a clean `config.yaml` with:
```yaml
server_port: <port>
idle_timeout_minutes: 30
max_sessions: 10
default_model: sonnet

autopilot_guards:
  max_budget_usd: 5.00
  max_turns_without_input: 50
  max_minutes_without_input: 30

providers: {}
models: {}
group_chat:
  default_moderator: ada
  available_minds: []

mcp_port: 7777
discord_allowed_users: []
discord_allowed_channels: []
telegram_allowed_users: []
telegram_owner_chat_id: 0
scheduled_tasks: []
```

Providers, models, users, and tasks are populated by their respective setup skills later.

## Step 4 — Generate .env

Write `.env` with only what docker-compose.yml requires:
```
COMPOSE_PROFILES=<selected-profile>
```

Additional variables are added by other setup skills as needed.

## Step 5 — Generate .mcp.json

Detect the project root path. Write `.mcp.json` with host paths and `.mcp.container.json` with container paths.

## Step 6 — Report

Files created, profile selected, any port remappings applied.
