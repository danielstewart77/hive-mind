# Secret Management

## Hierarchy

Secrets follow a strict priority order:

1. **System keyring** (primary) — `keyrings.alt.file.PlaintextKeyring`, data at `/home/hivemind/.claude/data/python_keyring/keyring_pass.cfg`, shared across containers via `.claude` bind mount
2. **Environment variables** (fallback) — for cases where keyring is unavailable
3. **`.env` file** (third-party only) — consumed exclusively by docker-compose interpolation for Planka (which cannot read from a keyring)

## Reading Secrets

Use `get_credential(key)` from `core/secrets.py`. It checks keyring first, env fallback, returns `None` if neither has the key. Never read secrets any other way.

## Keyring Configuration

All Python services set these env vars:
- `PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring`
- `XDG_DATA_HOME=/home/hivemind/.claude/data`

Service name for all keys: `hive-mind`.

## Keyring-to-Env Bridge

The gateway server (`server.py`) reads `HITL_INTERNAL_TOKEN` from keyring at startup and injects it into `os.environ` so Claude CLI subprocesses can resolve it.

## Managed Keys

DISCORD_BOT_TOKEN, TELEGRAM_BOT_TOKEN, HITL_INTERNAL_TOKEN, X_BEARER_TOKEN, PLANKA_EMAIL, PLANKA_PASSWORD, PLANKA_URL, LUCENT_BEARER_TOKEN, HIVE_TOOLS_TOKEN

## Keys Still in .env

These must stay in `.env` for docker-compose interpolation (third-party containers):
PLANKA_SECRET_KEY_BASE, PLANKA_BASE_URL, PLANKA_ADMIN_EMAIL, PLANKA_ADMIN_PASSWORD, PLANKA_ADMIN_NAME, PLANKA_ADMIN_USERNAME

## Rules

- Never hardcode secrets in source code
- Never put secrets in `.env` for Python services
- New secrets go in keyring via the `/secrets` skill
- Use `get_credential()` to read — never `os.getenv()` directly for secrets
- No `env_file: .env` on any Python service in docker-compose
