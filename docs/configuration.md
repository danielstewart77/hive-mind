# Configuration

## config.yaml

Non-secret settings live in `config.yaml`. Copy `config.yaml.example` to get started.

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

scheduled_tasks:
  - cron: "0 7 * * *"
    voice: true
    prompt: "Run /7am"
  - cron: "0 13 * * *"
    voice: false
    prompt: "Run /1pm"
  - cron: "0 3 * * *"
    voice: false
    prompt: "Run /3am"
```

### Fields

| Field | Default | Description |
|---|---|---|
| `server_port` | `8420` | Gateway HTTP port |
| `idle_timeout_minutes` | `30` | Kill sessions idle longer than this |
| `max_sessions` | `10` | Maximum concurrent Claude subprocesses |
| `default_model` | `sonnet` | Model alias to use when none specified |
| `providers` | — | Provider configs (see [Providers](providers.md)) |
| `models` | — | Map of model alias → provider name |
| `scheduled_tasks` | `[]` | Cron jobs run by the scheduler service |

### Scheduled Tasks

Each entry in `scheduled_tasks` is fired by the scheduler container at the given cron expression:

```yaml
scheduled_tasks:
  - cron: "0 7 * * *"   # 7am daily
    voice: true          # respond via voice (TTS)
    prompt: "Run /7am"   # message sent to Ada
```

## Secrets

All application secrets are stored in the system keyring (`keyrings.alt.file.PlaintextKeyring`), not in `.env` files. The keyring data lives at:

```
/home/hivemind/.claude/data/python_keyring/keyring_pass.cfg
```

This path is shared across containers via a bind mount on `${HOST_CLAUDE_DIR}`.

### Reading Secrets

Use `get_credential(key)` from `agents/secret_manager.py`:

```python
from agents.secret_manager import get_credential

api_key = get_credential("ANTHROPIC_API_KEY")  # keyring first, env fallback
```

### Writing Secrets

Use the `set_secret` MCP tool (available to Ada) or call `keyring.set_password("hive-mind", key, value)` directly.

### Required Secrets

| Key | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | Claude CLI subprocesses |
| `TELEGRAM_BOT_TOKEN` | Telegram client |
| `DISCORD_BOT_TOKEN` | Discord client |
| `MCP_AUTH_TOKEN` | Gateway ↔ hive_mind_mcp bearer auth |
| `HITL_INTERNAL_TOKEN` | HITL approval validation |
| `NEO4J_URI` | Knowledge graph connection |
| `PLANKA_EMAIL`, `PLANKA_PASSWORD`, `PLANKA_URL` | Kanban board |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | LinkedIn OAuth |

### .env File (Third-Party Only)

A minimal `.env` remains for docker-compose interpolation consumed by Neo4j and Planka (containers that cannot read from a keyring):

```env
NEO4J_AUTH=neo4j/your-password
PLANKA_DB_PASSWORD=your-db-password
PLANKA_SECRET_KEY_BASE=your-secret-key
PLANKA_ADMIN_EMAIL=admin@example.com
PLANKA_ADMIN_PASSWORD=changeme
PLANKA_ADMIN_NAME=Admin
PLANKA_ADMIN_USERNAME=admin
```

No application secret belongs in `.env`.

## Environment Variables (Per-Container)

Each container receives only the env vars it needs. Set in `docker-compose.yml`:

| Var | Containers | Purpose |
|---|---|---|
| `SESSIONS_DB_PATH` | server | SQLite database path |
| `PYTHON_KEYRING_BACKEND` | all Python services | Force PlaintextKeyring |
| `XDG_DATA_HOME` | server, bots | Keyring data directory |
| `HIVE_MIND_SERVER_URL` | bots, scheduler | Gateway URL |
| `VOICE_SERVER_URL` | bots | Voice server URL |
| `WHISPER_MODEL` | voice-server | Whisper model size |
| `USE_F5TTS` | voice-server | Enable F5-TTS (vs Kokoro) |
