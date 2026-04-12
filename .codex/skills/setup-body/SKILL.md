---
name: setup-body
description: Bootstrap Hive Mind body — communication surfaces, external integrations, voice, and optional services. Choose what to deploy. At least one communication surface is required.
---

# setup-body

Choose and deploy body components. At least one communication surface is needed.

## Step 1 — Prerequisite check

```bash
curl -sf http://localhost:8420/sessions > /dev/null || echo "Gateway not reachable. Run /setup-nervous-system first."
```

## Step 2 — Scan current state

```bash
for svc in discord telegram hivemind scheduler voice-server; do
  status=$(docker ps --filter name=hive-mind-$svc --format '{{.Status}}' 2>/dev/null)
  echo "$svc: ${status:-not deployed}"
done
```

## Step 3 — Present all components

**Communication Surfaces** (need at least one):
```
1. Discord bot          — Chat via Discord server
2. Telegram bot         — Chat via Telegram DM
3. Group chat bot       — Multi-mind group conversations via Telegram
4. Open WebUI           — Web chat interface (container)
5. Custom web chat      — Wire up your own web chat endpoint
```

**Automation:**
```
6. Scheduler            — Cron-based automated tasks
```
Note: The scheduler runs tasks on a schedule but is not a communication surface. If selected, you can configure scheduled tasks now or later.

**Voice:**
```
7. Voice server         — Speech-to-text + text-to-speech (GPU or CPU)
```

**External Tool Integrations** (via hive-mind-mcp):
```
8.  Gmail               — Send/read email (Google OAuth required)
9.  Google Calendar      — Calendar management (Google OAuth required)
10. Asana               — Project/task management
11. Docker management   — Container lifecycle (read-only or full access)
```

**Network mode** (asked before infrastructure):
```
Local only      — services on localhost, no TLS
Network exposed — public-facing, TLS via Caddy
```

**Optional Infrastructure:**
```
12. Caddy               — Reverse proxy + TLS (network-exposed only)
13. Planka              — Kanban board for task tracking
14. Nextcloud           — File storage and sync
15. Canvas (web UI)     — Web dashboard with markdown endpoints
```

```
Which components would you like to set up? (comma-separated numbers, or 'all')
```

## Step 4 — Deploy selected components

### Communication Surfaces (1-5)

**Discord bot (1):**
- Check DISCORD_BOT_TOKEN in keyring. If missing, ask user.
- Ask for allowed Discord user IDs, add to config.yaml.
- Deploy: `docker compose up -d discord-bot`
- Verify: `docker logs hive-mind-discord --tail 10 2>&1 | grep -i "ready\|logged in"`

**Telegram bot (2):**
- Check TELEGRAM_BOT_TOKEN in keyring. If missing, ask user.
- Ask for allowed Telegram user IDs and owner chat ID, add to config.yaml.
- Deploy: `docker compose up -d telegram-bot`
- Verify: `docker logs hive-mind-telegram --tail 10 2>&1 | grep -i "started\|polling"`

**Group chat bot (3):**
- Check HIVEMIND_TELEGRAM_BOT_TOKEN in keyring. If missing, ask user.
- Deploy: `docker compose up -d hivemind-bot`
- Verify via logs.

**Open WebUI (4):**
- Pull and run Open WebUI container:
  ```bash
  docker run -d --name open-webui --network hive_mind_net -p 3000:8080 ghcr.io/open-webui/open-webui:main
  ```
- Configure to connect to the gateway API.
- Verify: `curl -sf http://localhost:3000`

**Custom web chat (5):**
- Ask user for their existing endpoint URL.
- Help wire it to the gateway API (provide endpoint docs, auth token setup).
- Verify connectivity.

### Automation (6)

**Scheduler:**
- Deploy: `docker compose up -d scheduler`
- Ask: "Would you like to set up any scheduled tasks now?"
  - If yes, walk through cron schedule + prompt configuration, add to config.yaml `scheduled_tasks:`
  - If no: "You can add scheduled tasks later in config.yaml under `scheduled_tasks:`"

### Voice (7)

**Voice server:**
- Detect hardware profile (GPU or CPU-only).
- If GPU: "GPU detected. Voice server will use GPU-accelerated models (faster inference, larger models)."
- If CPU-only: "No GPU detected. Voice server will use CPU-only models (smaller Whisper model, CPU-friendly TTS). Inference will be slower but functional."
- Present the tradeoff and let user confirm.
- Deploy with appropriate config:
  ```bash
  docker compose --profile ${COMPOSE_PROFILES:-cpu-only} up -d voice-server
  ```
- Verify STT: test endpoint
- Verify TTS: test endpoint

### External Integrations (8-11)

Deploy hive-mind-mcp if not running:
```bash
docker compose up -d mcp
```
Check/store MCP_AUTH_TOKEN.

**Gmail (8):**
Full Google OAuth walkthrough:
1. Guide user to create a GCP project at https://console.cloud.google.com/
2. Enable the Gmail API
3. Create OAuth 2.0 credentials (Desktop app type)
4. Download `client_secret.json` to `hive_mind_mcp/credentials/`
5. Run the setup script on the host (not in Docker):
   ```bash
   cd /path/to/hive_mind_mcp && python3 setup_gmail.py
   ```
6. Browser opens for OAuth consent. Authorize.
7. Token saved to `credentials/token.json`.
8. Restart MCP container: `docker compose restart mcp`
9. Verify: test send/read via MCP tool

**Google Calendar (9):**
Same GCP project as Gmail. Enable Calendar API. The `setup_gmail.py` script handles both Gmail and Calendar tokens in one flow. If Gmail was already set up, Calendar may already be authorized.

**Asana (10):**
- Ask for Asana Personal Access Token (from https://app.asana.com/0/developer-console)
- Store in keyring
- Verify workspace access via Asana API

**Docker management (11):**
Present granularity options:
- **Read-only** — inspect, logs, ps only. Lower risk.
- **Full management** — start, stop, build, restart.
  - WARNING: "This gives minds the ability to manage Docker containers. Only enable if you understand the implications. A compromised mind with full Docker access could affect other containers."
- Configure Docker socket access in the MCP container accordingly.

### Optional Infrastructure (12-15)

**Caddy (12):** Only if network-exposed mode was selected.
- Ask for domain name
- Generate Caddyfile with automatic TLS
- Deploy: `docker compose up -d caddy`
- Verify TLS: `curl -sf https://<domain>`

**Planka (13):**
- Deploy Planka + Postgres: `docker compose up -d planka planka-db`
- Generate admin credentials, store in keyring
- Verify: `curl -sf http://localhost:1337`

**Nextcloud (14):**
- Deploy: `docker compose up -d nextcloud`
- Configure admin account
- Set up data directory volume
- Verify: `curl -sf http://localhost:8080`

**Canvas (15):**
- Ask: "Do you have an existing website?"
  - If yes: help wire a `/canvas` endpoint into it
  - If no: scaffold a minimal web app:
    - FastAPI + Jinja2 + markdown files as content endpoints
    - Based on the Spark to Bloom pattern
    - Ask: internal-only or public-facing?
    - Generate Dockerfile and docker-compose service
    - Deploy and verify

## Step 5 — Report

Status table for all selected components. Skip unselected ones.
