# Container Reference

Complete reference for all Hive Mind Docker services. Load this spec when building, debugging, or modifying containers.

## Services

### server (gateway)

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile` (Ubuntu 24.04, Python 3, Node.js, Claude Code CLI, Playwright) |
| Container | `hive-mind-server` |
| Port | `8420:8420` |
| Restart | `unless-stopped` |
| Command | `/opt/venv/bin/python3 server.py` |

**Volumes:**
| Mount | Path in container | Type | Purpose |
|-------|-------------------|------|---------|
| `${HOST_PROJECT_DIR:-.}` | `/usr/src/app` | Bind | Source code (dev hot-reload) |
| `${HOST_CLAUDE_DIR:-~/.claude}` | `/home/hivemind/.claude` | Bind | Claude keyring + config |
| `${HOST_CODEX_DIR:-~/.codex}` | `/home/hivemind/.codex` | Bind | Codex CLI config (Nagatha) |
| `sessions-db` | `/usr/src/app/data` | Named volume | SQLite sessions DB |
| `${HOST_MCP_DIR}` | `/usr/src/mcp` | Bind | External MCP project (set in .env) |
| `${HOST_SPARK_DIR}` | `/usr/src/spark_to_bloom` | Bind | External project (set in .env) |
| `${HOST_CADDY_DIR}` | `/usr/src/caddy` | Bind | Reverse proxy config (set in .env) |

**Environment:**
```
SESSIONS_DB_PATH=/usr/src/app/data/sessions.db
PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring
PYTHONNOUSERSITE=1
XDG_DATA_HOME=/home/hivemind/.claude/data
PYTHONPATH=/usr/src/app/vendor
```

**Security:** Full hardening (see [Security Settings](#security-settings))

**Special:** Has `tmpfs: /home/hivemind:uid=1000,gid=1000` because Claude Code writes `.claude.json` at startup. Without it, the container hangs silently on EROFS.

---

### discord-bot

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile` |
| Container | `hive-mind-discord` |
| Port | None (internal) |
| Restart | `unless-stopped` |
| Depends on | server, voice-server |
| Command | `/opt/venv/bin/python3 -m clients.discord_bot` |

**Volumes:** Source code bind + `.claude` bind (same as server, minus sessions-db and external projects).

**Environment:**
```
HIVE_MIND_SERVER_URL=http://server:8420
VOICE_SERVER_URL=http://voice-server:8422
PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring
XDG_DATA_HOME=/home/hivemind/.claude/data
```

---

### voice-server

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile.voice` |
| Container | `hive-mind-voice` |
| Port | `8422` (internal only) |
| Restart | `always` |
| GPU | NVIDIA, 1 device, `[gpu]` capabilities |
| Command | `/opt/venv/bin/python3 -m voice.voice_server` |

**Volumes:**
| Mount | Path in container | Type | Purpose |
|-------|-------------------|------|---------|
| `${HOST_PROJECT_DIR:-.}` | `/usr/src/app` | Bind | Source code |
| `whisper-cache` | `/home/hivemind/.cache` | Named volume | Whisper + Chatterbox models |

**Security:** `no-new-privileges`, `read_only`, `tmpfs: /tmp`. Omits `cap_drop: ALL` — required for NVIDIA GPU runtime.

---

### telegram-bot

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile` |
| Container | `hive-mind-telegram` |
| Port | None (internal) |
| Restart | `unless-stopped` |
| Depends on | server, voice-server |
| Command | `/opt/venv/bin/python3 -m clients.telegram_bot` |

Same volumes and environment as discord-bot.

---

### hivemind-bot

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile` |
| Container | `hive-mind-hivemind` |
| Port | None (internal) |
| Restart | `unless-stopped` |
| Depends on | server, voice-server |
| Command | `/opt/venv/bin/python3 -m clients.hivemind_bot` |

Group chat Telegram bot — routes messages through group sessions for multi-mind conversations. Same volumes and environment as discord-bot.

---

### scheduler

| Property | Value |
|----------|-------|
| Dockerfile | `Dockerfile` |
| Container | `hive-mind-scheduler` |
| Port | None (internal) |
| Restart | `unless-stopped` |
| Depends on | server, voice-server |
| Command | `/opt/venv/bin/python3 -m clients.scheduler` |

**Volumes:** `.claude` bind mount only (no source code bind — reads config from keyring).

---

### Lucent (knowledge graph + vector memory)

No separate container. Lucent is an embedded SQLite-backed graph and vector store that runs inside the main `hive-mind` container. Data persists via the `sessions-db` named volume (`/usr/src/app/data`).

---

### planka-db

| Property | Value |
|----------|-------|
| Image | `postgres:14-alpine` |
| Container | `hive-mind-planka-db` |
| Port | None (internal) |
| Restart | `unless-stopped` |

**Volumes:** `planka-db:/var/lib/postgresql/data`

---

### planka

| Property | Value |
|----------|-------|
| Image | `ghcr.io/plankanban/planka:latest` |
| Container | `hive-mind-planka` |
| Port | `3000:1337` |
| Restart | `unless-stopped` |
| Depends on | planka-db |

**Volumes:** `planka-data` mounted to avatars, backgrounds, and attachments.

---

## Network

All services share the `hivemind` bridge network (external, must exist before `docker compose up`):

```bash
docker network create hivemind
```

**Internal DNS resolution:**
| Hostname | Port | Protocol |
|----------|------|----------|
| `server` | 8420 | HTTP |
| `voice-server` | 8422 | HTTP |
| `planka-db` | 5432 | PostgreSQL |
| `planka` | 1337 | HTTP |

---

## Named Volumes

| Volume | Container path | Contents | Survives rebuild? |
|--------|---------------|----------|-------------------|
| `sessions-db` | `/usr/src/app/data` | SQLite sessions DB + Lucent graph/vector store | Yes |
| `planka-db` | `/var/lib/postgresql/data` | Kanban DB | Yes |
| `planka-data` | `/app/public/*`, `/app/private/attachments` | Kanban files | Yes |
| `whisper-cache` | `/home/hivemind/.cache` | Whisper STT + Chatterbox TTS models | Yes |

Named volumes survive `docker compose down`, image rebuilds, and container recreation. They are only destroyed by explicit `docker volume rm`.

---

## Security Settings

### Base hardening (all Python services)

```yaml
security_opt:
  - no-new-privileges:true
cap_drop:
  - ALL
read_only: true
tmpfs:
  - /tmp
```

### Exceptions

| Service | Exception | Reason |
|---------|-----------|--------|
| server | `tmpfs: /home/hivemind:uid=1000,gid=1000` | Claude Code writes `.claude.json` at startup; hangs without it |
| voice-server | No `cap_drop: ALL` | Required for NVIDIA GPU runtime access |

### Rules for new services

1. Always include all four base restrictions
2. If write access is needed, use `tmpfs` or a named volume — never remove `read_only`
3. Document any exceptions in this file
4. Test that the container starts cleanly — `read_only` causes silent hangs, not error messages

---

## Voice Server: Migration Plan (XTTS v2 -> Chatterbox)

### Current state (2026-03-15)
Running Chatterbox (ResembleAI) on Python 3.11-slim. Migrated from XTTS v2 (Coqui). Chatterbox produces better voice cloning quality, faster inference, and lower VRAM usage.

### Why Chatterbox
- Better voice cloning quality (zero-shot, WAV-only, no transcript needed)
- Faster inference (~3x real-time on A6000 vs ~1.5x for XTTS v2)
- Lighter VRAM (~2-3 GB vs ~4 GB for XTTS v2)
- Active development (ResembleAI)

### Engine history
| Date | Engine | Status |
|------|--------|--------|
| Mar 8 | F5-TTS + Kokoro | Working (transcript required) |
| Mar 12 | Fish Speech | Broken (model doesn't generate speech — see `fish.md`) |
| Mar 12 | Chatterbox | Working, proven via CLI test. Build lost on restart. |
| Mar 15 | XTTS v2 (Coqui) | Replaced by Chatterbox |
| Mar 15 | Chatterbox | Active -- migrated from XTTS v2 |

### Migration steps

#### 1. Update `Dockerfile.voice` for Chatterbox

Key constraints discovered during the Mar 12 build:
- **Python 3.12 is fine** (can switch from 3.11-slim to match main Dockerfile, or stay on 3.11)
- **`chatterbox-tts` pins `numpy<1.26`** — incompatible with Python 3.12 wheels. Install with `--no-deps` and provide deps separately.
- **`setuptools<81` required** — `resemble-perth` (Chatterbox dep) uses `pkg_resources`, removed in setuptools 82+
- **`chatterbox-tts` pins `torch==2.6.0`** — different from XTTS's `torch==2.5.1`

```dockerfile
FROM python:3.11-slim
# ... system deps ...

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip "setuptools<81" wheel

COPY requirements.voice.txt .
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.voice.txt
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps chatterbox-tts

# Build-time validation
RUN /opt/venv/bin/python -c "\
from chatterbox.tts import ChatterboxTTS; \
from faster_whisper import WhisperModel; \
print('Voice deps OK')"
```

#### 2. Update `requirements.voice.txt`

Replace Coqui/XTTS deps with Chatterbox deps (relaxed numpy):
```
# STT
faster-whisper>=1.0.0

# Chatterbox TTS deps (chatterbox-tts itself installed --no-deps in Dockerfile)
numpy
torch==2.6.0
torchaudio==2.6.0
librosa==0.11.0
s3tokenizer
transformers==4.46.3
diffusers==0.29.0
resemble-perth==1.0.1
conformer==0.3.2
safetensors==0.5.3
spacy-pkuseg
pykakasi==2.3.0
pyloudnorm
omegaconf

# Audio
soundfile

# HTTP server
fastapi
uvicorn[standard]
python-multipart
```

#### 3. Update `voice_server.py`

Replace XTTS synthesis with Chatterbox (see working code from Mar 12 session in `chatterbox.md`).

#### 4. Update `docker-compose.yml`

- Remove `tts-models` volume (XTTS cache — not needed for Chatterbox)
- Change env vars: remove `XTTS_*`, `COQUI_TOS_AGREED`
- `whisper-cache` volume covers Chatterbox model cache too (`~/.cache/huggingface/`)

#### 5. Clean up main `requirements.txt`

Remove chatterbox/torch deps from main requirements — those belong only in `requirements.voice.txt`. The main server image doesn't need torch.

#### 6. Commit and verify

```bash
docker compose -p hive_mind up -d --build voice-server
docker compose -p hive_mind logs voice-server --tail=20
# Verify: "Voice server ready. TTS: Chatterbox"
# Then commit ALL changed files before ending session
```

---

## Per-Client Voice References

### Goal
Each client (Telegram bot, Discord bot, future bots) gets its own voice. Multiple Telegram bots = multiple personalities = multiple voices. This is the foundation for the multi-mind hive.

### Design

#### Voice registry: `voice_ref/` directory
```
voice_ref/
  ada.wav           # Ada's voice (current hive_mind_voice.wav, Joanna Lumley)
  spark.wav         # Future bot voice
  oracle.wav        # Future bot voice
  default.wav       # Symlink to ada.wav (fallback)
```

Each voice is a ~10s WAV clip. No transcripts needed (Chatterbox is WAV-only).

#### TTS API: `voice_id` parameter

The `/tts` endpoint accepts an optional `voice_id`:

```python
class TTSRequest(BaseModel):
    text: str
    voice_id: str = "default"   # maps to voice_ref/{voice_id}.wav
    speed: float = 1.0
```

The voice server:
1. On startup, scans `voice_ref/` and caches all available voice IDs
2. On TTS request, looks up `voice_ref/{voice_id}.wav`
3. Falls back to `default.wav` if the requested voice doesn't exist
4. Chatterbox's `generate(text, audio_prompt_path=ref_path)` already accepts per-call ref audio — no model reload needed

#### Client-side: bot passes its voice ID

Each bot knows its own voice ID via env var:

```yaml
# docker-compose.yml
telegram-bot:
  environment:
    - VOICE_ID=ada

telegram-bot-spark:
  environment:
    - VOICE_ID=spark
```

The bot includes `voice_id` in every TTS request to the voice server. The voice server does not need to know or care which bot is calling — it just resolves the voice file.

#### Adding a new voice

1. Drop a ~10s WAV clip into `voice_ref/{name}.wav`
2. Set `VOICE_ID={name}` on the bot's container
3. No restart of voice-server needed — it discovers new files on each request (or on a periodic scan)

---

## Gotchas & Lessons Learned

### Model cache directories must be named volumes
Libraries download large models to user-writable paths. In a `read_only` container, these paths need named volumes or the container crashes on first download. Known paths:
- **Whisper:** `~/.cache/huggingface/` (covered by `whisper-cache`)
- **Chatterbox:** `~/.cache/huggingface/` (same volume)
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

**The fix:** Any change to `requirements*.txt`, a `Dockerfile`, or `docker-compose.yml` must be followed immediately by `compose up -d --build <service>` and a health check before ending the session.

### Restarting a stale container deploys previously uncommitted code

If code was changed (bind mount updated) but the image was never rebuilt, the container appears to work. On the next restart, it picks up the current code from disk — which may be a different version than what the old image's venv supports. This is how a simple `compose restart` can unexpectedly break a working service.

**Prevention:** Never leave a container running on a stale image. After any code + dependency change, rebuild immediately.

### Disk space kills everything
A full disk prevents container startup, image builds, and even debugging tools. The voice server's large model downloads (~4 GB) can fill a disk during rebuild. Monitor disk space before rebuilding ML containers.

---

## MCP Configuration

### Host (`.mcp.json`) — for Claude Code CLI on host
```json
{
  "mcpServers": {
    "hive-mind-tools": {
      "command": "<project-root>/venv/bin/python",
      "args": ["<project-root>/mcp_server.py"]
    }
  }
}
```
Runs MCP server locally. Lucent uses the `sessions-db` volume path; ensure `DATA_DIR` env var points to the correct host path if running outside Docker.

### Container (`.mcp.container.json`) — for services inside Docker
```json
{
  "mcpServers": {
    "hive-mind-tools": {
      "command": "/opt/venv/bin/python3",
      "args": ["/usr/src/app/mcp_server.py"]
    }
  }
}
```
Uses container paths. Lucent resolves via the shared `sessions-db` volume.

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
