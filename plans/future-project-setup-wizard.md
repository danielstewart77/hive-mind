# Future Project: User-Friendly Setup & Configuration Wizard

## Intention

Make Hive Mind installable and runnable by anyone — not just someone who already knows the internals. Today, standing up a fresh instance requires manually configuring keyring secrets, editing `.env`, knowing which Docker Compose files to combine, and understanding the architecture. This project replaces all of that with a guided setup experience.

---

## What It Needs to Do

### 1. Interactive Setup Script (`setup.sh` or `setup.py`)

Runs once on a fresh clone or after a reset. Guides the user through every required and optional configuration item:

- **Required secrets** (prompts for each, writes to keyring via `set_secret`):
  - `ANTHROPIC_API_KEY`
  - `TELEGRAM_BOT_TOKEN`
  - `DISCORD_BOT_TOKEN`
  - `NEO4J_AUTH`
  - `NEO4J_URI`
  - `PLANKA_EMAIL`, `PLANKA_PASSWORD`, `PLANKA_URL`
  - `MCP_AUTH_TOKEN`, `HITL_INTERNAL_TOKEN`
  - `X_BEARER_TOKEN`
  - `GITHUB_TOKEN`

- **Optional integrations** (yes/no prompts that toggle features):
  - Voice (F5-TTS / Whisper) — GPU required, optional
  - Discord bot
  - Telegram bot
  - Planka Kanban
  - Neo4j knowledge graph
  - X/Twitter lurker
  - Scheduler (cron jobs)

- **Provider selection**:
  - Anthropic (default, requires API key)
  - Ollama (local/private, prompts for base URL — skips Anthropic API key requirement)
  - Both (Anthropic as default, Ollama as fallback or per-session override)

- **Environment generation**:
  - Writes a `.env` file for docker-compose interpolation (only non-secret, third-party values)
  - Generates a `config.yaml` or patches the existing one based on selected options

### 2. GPU Detection & Hardware Profiling

Before prompting about optional features, the setup script runs a hardware probe and presents the user with what was found and what that enables:

```
Probing hardware...
  CPU:  AMD Ryzen 9 5900X (12-core)
  RAM:  64 GB
  GPU:  NVIDIA RTX A6000 — 48 GB VRAM  ✓ CUDA detected
        Driver: 535.104  CUDA: 12.2

GPU-accelerated features available:
  ✓ Voice (F5-TTS + Whisper) — recommended, sufficient VRAM
  ✓ Ollama (local LLM inference) — sufficient VRAM for 13B+ models
  ✓ Embedding model (qwen3-embedding:8b) — used by semantic memory
```

**Detection method:**
- `nvidia-smi` for NVIDIA GPUs — parse device name, VRAM, driver, CUDA version
- `rocm-smi` for AMD GPUs (ROCm)
- `lspci | grep -i vga` as a fallback identifier
- If no GPU found: CPU-only mode, voice and Ollama marked as unsupported

**Recommendation logic:**

| VRAM | Recommendation |
|------|---------------|
| < 4 GB | No GPU features — use Anthropic API only |
| 4–8 GB | Whisper STT only (no F5-TTS), small Ollama models (7B) |
| 8–16 GB | Whisper + F5-TTS voice, Ollama 7B–13B models |
| 16–24 GB | Full voice stack, Ollama 13B–30B, embedding model |
| 24 GB+ | Everything — full voice, large Ollama models, embedding model simultaneously |

The script uses these thresholds to auto-recommend a configuration, then lets the user accept or override each item individually.

**Docker `deploy.resources` block:**
The setup script writes the appropriate GPU reservation into `docker-compose.yml` (or an override file) based on detected hardware:

```yaml
# GPU capable — written automatically
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

If no GPU is detected, this block is omitted and voice/Ollama services are disabled automatically.

---

### 3. Ollama Configuration

Ollama can run in two modes:

**Remote Ollama** (existing server on the network):
- Setup prompts: "Do you have an Ollama server running?" → yes → enter base URL (e.g. `http://203.0.113.10:11434`)
- Script pings `/api/tags` to confirm connectivity and lists available models
- User selects which model to use as default (or accepts the recommended one)
- No GPU required on the Hive Mind host

**Local Ollama** (run Ollama inside Docker as a service):
- Requires GPU (gated by detection above)
- Setup adds an `ollama` service to Docker Compose
- Script pre-pulls recommended models based on available VRAM (see table above)
- Embedding model (`qwen3-embedding:8b`) pulled automatically if Neo4j/semantic memory is enabled

**config.yaml output (example):**
```yaml
providers:
  anthropic: {}          # omitted if Anthropic not selected
  ollama:
    env:
      ANTHROPIC_AUTH_TOKEN: "ollama"
      ANTHROPIC_BASE_URL: "http://ollama:11434"   # container name if local
    api_base: "http://ollama:11434"

ollama:
  mode: local            # or "remote"
  base_url: "http://ollama:11434"
  default_model: "llama3.1:8b"
  embedding_model: "qwen3-embedding:8b"
```

---

### 4. Voice Configuration

Voice is split into two independent components, each optional:

**STT (Speech-to-Text) — Whisper:**
- Runs on GPU if available, CPU fallback possible (slow but functional)
- Model size options presented based on VRAM:
  - `tiny` / `base` — CPU-safe, fast, lower accuracy
  - `small` / `medium` — 4–8 GB VRAM, good accuracy
  - `large-v3` — 10+ GB VRAM, best accuracy (current default on our A6000)
- If CPU-only: warn about latency, let user decide

**TTS (Text-to-Speech) — F5-TTS:**
- Requires GPU (CUDA). CPU inference is technically possible but impractically slow for real-time use — setup should warn and gate this behind explicit confirmation if no GPU found
- Voice cloning requires a reference `.wav` file — setup prompts for path or offers to use the default Joanna Lumley reference clip
- If GPU not available: offer fallback to a simpler CPU-capable TTS (e.g. piper-tts) as a degraded-but-functional alternative

**Voice config block in `config.yaml`:**
```yaml
voice:
  enabled: true
  stt:
    enabled: true
    model: large-v3        # auto-selected based on VRAM
    device: cuda
  tts:
    enabled: true
    engine: f5-tts         # or "piper" for CPU fallback
    reference_clip: voice_ref/hive_mind_voice.wav
    device: cuda
```

---

### 6. Feature Flags in `config.yaml`

Extend `config.yaml` to have an `enabled_services` section:

```yaml
enabled_services:
  discord: true
  telegram: true
  voice: false
  scheduler: true
  neo4j: true
  planka: true
```

`docker-compose.yml` reads these via an override or profile mechanism so disabled services don't start at all. The server and MCP layer respect the flags at runtime — tools for disabled services return a graceful "not configured" message instead of crashing.

### 7. Health Check / Validation Pass

After setup, a `validate.sh` or `--check` flag that:

- Confirms all required keyring secrets are present
- Tests connectivity to each enabled external service (Neo4j ping, Telegram getMe, Discord auth, etc.)
- Reports a green/yellow/red status per component
- Suggests fixes for anything yellow/red

### 8. Docker Compose Profile Integration

Today: `docker compose up -d --build` starts everything unconditionally.

After this project: Services are tagged with Compose profiles (`voice`, `discord`, `telegram`, etc.). The setup script writes a `.compose-profile` file or sets `COMPOSE_PROFILES` in `.env` so only enabled services start.

```yaml
# docker-compose.yml
services:
  voice-server:
    profiles: ["voice"]
    ...
  discord-bot:
    profiles: ["discord"]
    ...
```

### 9. Re-run / Update Flow

`setup.sh --update` to add a new secret or toggle a feature without re-entering everything. Shows current configuration state and lets user change specific items.

### 10. Documentation / README

A `SETUP.md` that describes:
- Prerequisites (Docker, Docker Compose, GPU optional)
- Clone → run setup.sh → docker compose up
- How to add secrets after the fact
- How to enable/disable optional components

---

## Implementation Notes

- The setup script should use Python (for keyring access) or bash with a Python helper for keyring writes
- `setup.py` can import `agents/secret_manager.py` directly for keyring operations
- Config patching should use a proper YAML library (ruamel.yaml to preserve comments)
- Avoid requiring the user to understand the internal architecture — abstract everything behind plain-English prompts
- Consider a web UI (served on localhost during setup only) as a future enhancement, but CLI first

---

## Priority / Sequencing

This is a good project to pick up after the core system is stable. It doesn't change any runtime behavior — purely additive. The Docker Compose profiles piece is the highest-value quick win and could be done independently as a precursor.

**Depends on:** Nothing in the current backlog.
**Enables:** Hive Mind as a distributable project, not just a personal instance.
