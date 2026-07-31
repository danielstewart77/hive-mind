# Secret Management

## Hierarchy

Secrets come from two real, live sources — not a keyring-with-third-party-.env-exception model:

1. **System keyring** (primary for skill/tool-level secrets) — `core.keyring_backend.HiveMindKeyring`, a `keyrings.alt.file.PlaintextKeyring` subclass whose storage path comes directly from the `KEY_RING` env var (e.g. `/usr/src/app/data/keyring`), not `XDG_DATA_HOME`.
2. **`.env` files** (primary for service-to-service bearer tokens) — two separate files, both consumed via Docker Compose `env_file:` by real Python services: repo root `.env` (bot tokens, `COMMS_BEARER_TOKEN`) and `nervous-system/.env` (`LUCENT_BEARER_TOKEN`, `COMMS_BEARER_TOKEN` — must match the root value, plus the two admin tokens). `lucent` and `comms` both have `env_file: ./nervous-system/.env` in `docker-compose.yml` — this is not a third-party-only exception.
3. **Environment variable fallback** — `get_credential()` falls back to `os.getenv()` when keyring lookup fails or returns nothing, which is how a plain `.env`-sourced value satisfies code that calls `get_credential()`.

## Reading Secrets

Use `get_credential(key)` from `core/secrets.py`. It checks keyring first, falls back to `os.getenv()`, returns `None` if neither has the key.

## Keyring Configuration

Python services that use the keyring set:
- `PYTHON_KEYRING_BACKEND=core.keyring_backend.HiveMindKeyring`
- `KEY_RING=/usr/src/app/data/keyring` (or wherever the deployment mounts it)

Service name for all keys: `hive-mind`.

## Managed Keys — actually required for a basic deployment

`COMMS_BEARER_TOKEN` (root `.env` **and** `nervous-system/.env` — must match), `LUCENT_BEARER_TOKEN` (`nervous-system/.env`), `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` (root `.env`, whichever surface you run).

## Managed Keys — feature-specific, only if you're using them

`HIVE_TOOLS_TOKEN`, `MCP_AUTH_TOKEN`, `X_BEARER_TOKEN`, `LINKEDIN_CLIENT_ID`/`LINKEDIN_CLIENT_SECRET`, `LUCENT_ADMIN_BEARER_TOKEN`/`COMMS_ADMIN_BEARER_TOKEN`, `SMS_INBOUND_HMAC_SECRET`.

## Rules

- Never hardcode secrets in source code
- New keyring-managed secrets go in via the `/secrets` skill
- Use `get_credential()` to read a keyring-first secret — never `os.getenv()` directly when a value might legitimately live in the keyring
- Bearer tokens that gate service-to-service auth (`COMMS_BEARER_TOKEN`, `LUCENT_BEARER_TOKEN`, and their admin variants) are `.env`-only by design, not keyring — they need to be readable by Docker Compose itself for `env_file:` interpolation
