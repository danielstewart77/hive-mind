# Configuration

## config.yaml

Non-secret bot/scheduler settings live in `config.yaml`. Copy `config.yaml.example` to get started. This is **not** the gateway's own config — see [Providers](providers.md) for `nervous-system/comms/config.yaml`, the file that actually drives model→provider resolution.

```yaml
telegram_allowed_users:
  - 0000000000
telegram_owner_chat_id: 0000000000

discord_allowed_users:
  - 000000000000000000
discord_allowed_channels: []
```

### Fields actually read by the current bots

| Field | Read by | Description |
|---|---|---|
| `telegram_allowed_users` | `bots/telegram_bot.py` | Allowlisted Telegram user IDs |
| `telegram_owner_chat_id` | `bots/scheduler.py` | DM chat ID for scheduler-fired notifications |
| `discord_allowed_users` | `bots/discord_bot.py` | Allowlisted Discord user IDs |
| `discord_allowed_channels` | `bots/discord_bot.py` | Allowlisted channels (empty = all channels + DMs) |

### Fields in `config.yaml.example` with no current reader

`server_port`, `idle_timeout_minutes`, `max_sessions`, `default_model`, `autopilot_guards`, `mcp_port`, `providers`, `models` are parsed by `config.py` but nothing in this repo reads the parsed values — they're left over from the retired `server.py` gateway, which owned all of this before the gateway moved to `hive-comms`. `group_chat` is parsed and read by `nervous-system/comms/inter_mind_api/`, which is dead code (not built by any Dockerfile, not started by any compose service — group chat currently has no working end-to-end path). None of these currently do anything; treat them as reserved, not configuration you need to tune.

### Scheduled Tasks

Schedules are declared in skill frontmatter, not in `config.yaml`. See [Scheduled Tasks](scheduled-tasks.md).

## Secrets

Secrets are stored in the system keyring (primary) or a `.env` file (real fallback — not third-party-only; see below), retrieved via `get_credential(key)` from `core/secrets.py`:

```python
from core.secrets import get_credential

token = get_credential("HIVE_TOOLS_TOKEN")  # keyring first, env fallback
```

Keyring storage path is controlled by `KEY_RING` (e.g. `/usr/src/app/data/keyring`), backed by `core.keyring_backend.HiveMindKeyring` — set `PYTHON_KEYRING_BACKEND=core.keyring_backend.HiveMindKeyring` in the container environment.

### Writing Secrets

Use the `/secrets` skill, or `keyring.set_password("hive-mind", key, value)` directly.

### `.env` files — real, not just for third parties

Two separate `.env` files hold real secrets, both consumed via Docker Compose `env_file:`:

- Repo root `.env` (copy from `.env.example`) — `COMMS_BEARER_TOKEN`, bot tokens, SMTP.
- `nervous-system/.env` (copy from `nervous-system/.env.example`) — `LUCENT_BEARER_TOKEN`, `COMMS_BEARER_TOKEN` (must match the root value — comms checks incoming requests against it), the two admin bearer tokens.

Planka's own admin credentials (`PLANKA_DB_PASSWORD`, `PLANKA_SECRET_KEY_BASE`, `PLANKA_ADMIN_*`) are the one case that's genuinely third-party-only — Planka can't read a keyring, so those five stay `.env`-only regardless.

### Secrets actually required for a basic single-mind deployment

| Key | Where | Used by |
|---|---|---|
| `COMMS_BEARER_TOKEN` | root `.env` **and** `nervous-system/.env` (must match) | Bots authenticating to hive-comms; comms authenticating requests |
| `LUCENT_BEARER_TOKEN` | `nervous-system/.env` | hive-lucent request auth |
| `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` | root `.env` | Whichever surface(s) you run |
| Claude Code credentials | mounted from `HOST_CLAUDE_DIR` (host `~/.claude`), not a keyring secret | Every mind's Claude CLI subprocess |

`HIVE_TOOLS_TOKEN`, `MCP_AUTH_TOKEN`, `LINKEDIN_CLIENT_ID`/`SECRET`, and Planka's vars are only needed if you're actually running those integrations.
