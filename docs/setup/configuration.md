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

### Scheduled Tasks

Schedules are declared in skill frontmatter, not in `config.yaml`. See [Scheduled Tasks](scheduled-tasks.md).

## Secrets

All application secrets are stored in the system keyring, not in `.env` files. The keyring data lives at:

```
/usr/src/app/data/keyring/python_keyring/keyring_pass.cfg
```

This path is controlled by the `KEY_RING` env var and is bind-mounted from the host via `HOST_PROJECT_DIR`.

The keyring backend is `core.keyring_backend.HiveMindKeyring` — a subclass of `PlaintextKeyring` that reads the storage path directly from `KEY_RING` without overloading `XDG_DATA_HOME`. Set via `PYTHON_KEYRING_BACKEND=core.keyring_backend.HiveMindKeyring` in docker-compose.

### Reading Secrets

Use `get_credential(key)` from `core/secrets.py`:

```python
from core.secrets import get_credential

api_key = get_credential("ANTHROPIC_API_KEY")  # keyring first, env fallback
```

### Writing Secrets

Use the `/secrets` skill or call `keyring.set_password("hive-mind", key, value)` directly.

### Required Secrets

| Key | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | Claude CLI subprocesses |
| `TELEGRAM_BOT_TOKEN` | Telegram client |
| `DISCORD_BOT_TOKEN` | Discord client |
| `MCP_AUTH_TOKEN` | Gateway ↔ hive_mind_mcp bearer auth |
| `HITL_INTERNAL_TOKEN` | HITL approval validation |
| `PLANKA_EMAIL`, `PLANKA_PASSWORD`, `PLANKA_URL` | Kanban board |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | LinkedIn OAuth |

### .env File (Third-Party Only)

A minimal `.env` remains for docker-compose interpolation consumed by Planka (which cannot read from a keyring):

```env
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
| `PYTHON_KEYRING_BACKEND` | all Python services | Set to `core.keyring_backend.HiveMindKeyring` |
| `KEY_RING` | all Python services | Keyring storage root (e.g. `/usr/src/app/data/keyring`) |
| `HIVE_MIND_SERVER_URL` | bots, scheduler | Gateway URL |
| `VOICE_SERVER_URL` | bots | Voice server URL |
| `WHISPER_MODEL` | voice-server | Whisper model size |
| `TTS_BACKEND` | voice-server | TTS engine: `chatterbox` (default) or `bark` |
| `VOICE_REF_DIR` | voice-server | Directory containing per-mind `{voice_id}.wav` reference clips |
