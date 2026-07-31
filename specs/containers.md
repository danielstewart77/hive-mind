# Container Reference

Complete reference for all Hive Mind Docker services. Load this spec when building, debugging, or modifying containers. Reflects `docker-compose.example.yml` — a live deployment's own `docker-compose.yml` adds its own mind fragments via `include:`.

## Services

### lucent (vector store + knowledge graph)

| Property | Value |
|----------|-------|
| Build | `context: ./nervous-system` |
| Container | `hive-lucent` |
| Port | `0.0.0.0:8425:8424` |
| Restart | `unless-stopped` |

**Volumes:** `./nervous-system/data:/data` (SQLite store), `./nervous-system/lucent_api:/app/lucent_api` (bind-mounted source, no rebuild needed for hot fixes).

**Environment:** `LUCENT_API_PORT`, `LUCENT_URL_SELF`, `LUCENT_DB_PATH`, `OLLAMA_BASE_URL`, `HIVE_TOOLS_URL`, `PRUNE_LOG_PATH`/`PRUNE_CRON`/`PRUNE_TIMEZONE` plus `./nervous-system/.env` (`LUCENT_BEARER_TOKEN`, `LUCENT_ADMIN_BEARER_TOKEN`).

**Security:** none of the base hardening below is applied to this service.

### comms (gateway)

| Property | Value |
|----------|-------|
| Build | `context: ./nervous-system, dockerfile: comms/Dockerfile` |
| Container | `hive-comms` |
| Port | `0.0.0.0:8426:8424` |
| Restart | `unless-stopped` |

**Volumes:** `./nervous-system/data:/data` (broker + sessions DB), `./nervous-system/comms:/app/comms` (bind-mounted source).

**Environment:** `COMMS_PORT`, `COMMS_PROJECT_DIR`, `BROKER_DB_PATH`, `SESSIONS_DB_PATH` plus `./nervous-system/.env` (`COMMS_BEARER_TOKEN`, `COMMS_ADMIN_BEARER_TOKEN`).

**Security:** none of the base hardening below is applied to this service.

### Mind containers

Not listed individually here — each mind is `include:`-ed from its own `minds/<name>/container/compose.yaml` fragment (see [docs/architecture/mind-folder-contract.md](../docs/architecture/mind-folder-contract.md)). Every mind runs the same shared harness image/command (`minds.harness.claude_cli` or `minds.harness.codex_cli`), selected per-fragment, pointed at that mind's own `runtime.yaml` via `MIND_NAME`. No hardening is applied to mind containers in `docker-compose.example.yml` today.

### telegram-bot

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile` |
| Container | `hive-mind-telegram` |
| Port | None (internal) |
| Restart | `unless-stopped` |
| Depends on | comms, voice-server |
| Command | `/opt/venv/bin/python3 -m bots.telegram_bot` |

**Environment:** `HIVE_MIND_SERVER_URL=http://hive-comms:8424`, `COMMS_BEARER_TOKEN`, `VOICE_SERVER_URL=http://voice-server:8422`, `MIND_ID`, `TELEGRAM_BOT_TOKEN_KEYRING_KEY`.

**Security:** `no-new-privileges`, `cap_drop: ALL`, `read_only`, `tmpfs: /tmp`.

### discord-bot

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile` |
| Container | `hive-mind-discord` |
| Port | None (internal) |
| Restart | `unless-stopped` |
| Depends on | comms, voice-server |
| Command | `/opt/venv/bin/python3 -m bots.discord_bot` |

Same environment shape and hardening as telegram-bot (minus the keyring-key var).

### voice-server

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile.voice` |
| Container | `hive-mind-voice` |
| Port | `8422:8422` |
| Restart | `always` |
| GPU | NVIDIA, 1 device, `[gpu]` capabilities |
| Command | `/opt/venv/bin/python3 -m voice.voice_server` |

**Volumes:** `${HOST_PROJECT_DIR:-.}:/usr/src/app`, `whisper-cache:/home/hivemind/.cache` (model downloads — see chatterbox.md).

**Security:** `no-new-privileges`, `read_only`, `tmpfs: /tmp`. Omits `cap_drop: ALL` — required for NVIDIA GPU runtime.

### voice-server-kokoro

Same image/Dockerfile pattern as voice-server (`Dockerfile.voice.kokoro`), container `hive-mind-voice-kokoro`, host port `8423:8422`, `kokoro-cache` named volume, `TTS_ENGINE=kokoro`. Fast, non-cloning TTS for minds that don't need a cloned voice — point a mind's `VOICE_SERVER_URL` here instead of `voice-server` to use it.

### scheduler

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile` |
| Container | `hive-mind-scheduler` |
| Port | None (internal) |
| Restart | `unless-stopped` |
| Command | `/opt/venv/bin/python3 -m bots.scheduler` |

Walks `minds/*/.claude/skills/*/SKILL.md` at startup and registers a job per `schedule:`-declared skill.

---

## Network

All services share the `hivemind` bridge network (external, must exist before `docker compose up`):

```bash
docker network create hivemind
```

**Internal DNS resolution:**
| Hostname | Port | Protocol |
|----------|------|----------|
| `hive-lucent` | 8424 | HTTP |
| `hive-comms` | 8424 | HTTP |
| `voice-server` | 8422 | HTTP |
| `voice-server-kokoro` | 8422 | HTTP |

---

## Named Volumes

| Volume | Container path | Contents |
|--------|---------------|----------|
| `whisper-cache` | `/home/hivemind/.cache` | Whisper STT + Chatterbox TTS models |
| `kokoro-cache` | `/home/hivemind/.cache` | Kokoro TTS models |

`nervous-system/data/` (lucent + broker + sessions DBs) is a bind mount, not a named volume — see the `lucent`/`comms` sections above.

---

## Security Settings

See [container-hardening.md](container-hardening.md) for the current, honest picture: hardening is applied to the bots and voice servers only, not to mind containers, `lucent`, or `comms`.

---

## Gotchas & Lessons Learned

### Model cache directories must be named volumes
Libraries download large models to user-writable paths. In a `read_only` container, these paths need named volumes or the container crashes on first download. Known paths:
- **Whisper / Chatterbox:** `~/.cache/huggingface/` (covered by `whisper-cache`)
- **Kokoro:** same pattern, covered by `kokoro-cache`
- **Matplotlib:** `~/.config/matplotlib/` (falls back to `/tmp`, non-fatal warning)

If adding a new ML model, find where it caches and add a volume before deploying.

### Dockerfile must pre-create volume mount points with correct ownership
Docker initializes named volumes from the container's filesystem. If the directory doesn't exist or is owned by root, the volume inherits wrong permissions and the non-root user gets `Permission denied`. Always create the directory and `chown` it in the Dockerfile:
```dockerfile
RUN mkdir -p /home/hivemind/.cache \
    && chown -R hivemind:hivemind /home/hivemind
```

### Pin ML dependency versions
Unpinned ML packages (`transformers`, `torch`) regularly ship breaking changes. Always pin exact versions or upper bounds in requirements files.

### `compose restart` does NOT deploy new code or packages

`docker compose restart` stops and starts the existing container with the **same image**. It does not rebuild. Code changes on the bind mount (`/usr/src/app`) take effect immediately on restart, but venv changes (`requirements.txt`, new packages) do NOT — the venv is baked into the image layer and only changes on `compose up --build`.

**The failure mode:** You add a package to `requirements.txt`, commit, and restart. The container picks up the new code (bind mount) but the old venv. The import fails at runtime, not at deploy time. The container crash-loops with a `ModuleNotFoundError`.

**The fix:** Any change to `requirements*.txt`, a `Dockerfile`, or a compose file must be followed immediately by `compose up -d --build <service>` and a health check before ending the session.

### Restarting a stale container deploys previously uncommitted code

If code was changed (bind mount updated) but the image was never rebuilt, the container appears to work. On the next restart, it picks up the current code from disk — which may be a different version than what the old image's venv supports. This is how a simple `compose restart` can unexpectedly break a working service.

**Prevention:** Never leave a container running on a stale image. After any code + dependency change, rebuild immediately.

### Disk space kills everything
A full disk prevents container startup, image builds, and even debugging tools. The voice server's large model downloads (~4 GB) can fill a disk during rebuild. Monitor disk space before rebuilding ML containers.

---

## Common Operations

```bash
# Start everything
docker compose up -d --build

# Rebuild a single service (no downtime for others)
docker compose up -d --build voice-server

# View logs
docker compose logs -f voice-server --tail=50

# Check all service status
docker compose ps

# Nuclear restart (preserves volumes)
docker compose down && docker compose up -d --build

# Actually destroy volumes (DATA LOSS)
docker compose down -v
```
