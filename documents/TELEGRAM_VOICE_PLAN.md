# Telegram Bot + Voice Pipeline Plan

## Overview

Add Telegram as a second client to Hive Mind, with full two-way voice support powered
by a local TTS model on the RTX A6000 (48GB VRAM). Architecture mirrors the existing
Discord bot — thin HTTP client connecting to the gateway — with an additional voice
pipeline layer for STT and TTS.

---

## Architecture

```
Telegram User
  │
  ├── Text message
  │     └── POST /sessions/{id}/message → SSE response → send text reply
  │
  └── Voice message (OGG/Opus)
        ├── Download audio file from Telegram
        ├── Convert OGG → WAV (ffmpeg)
        ├── Transcribe WAV → text (faster-whisper, local GPU)
        ├── POST /sessions/{id}/message → SSE response
        ├── Synthesise text → audio (TTS server, local GPU)
        └── Send audio back as Telegram voice note (OGG/Opus)
```

---

## Components

### 1. `telegram_bot.py`
Thin client mirroring `discord_bot.py`. Handles:
- Text in / text out (identical to Discord flow)
- Voice message in → transcribe → gateway → TTS → voice out
- All slash commands routed to gateway `/command` endpoint
- Session isolation per Telegram chat ID

### 2. STT — faster-whisper
- **Model:** `large-v3` (runs fine on A6000, best accuracy)
- **Why:** Local, free, Apache 2.0, excellent accuracy, GPU-accelerated
- **VRAM:** ~3GB for large-v3
- **Speed:** ~10-20x real-time on A6000
- **Install:** `pip install faster-whisper`

### 3. TTS Server (see TTS section below)
- Runs as a separate local HTTP service
- `telegram_bot.py` calls it via `POST /tts` with text, receives audio bytes
- Keeps bot code clean and TTS model swappable

---

## TTS Model Selection (RTX A6000, 48GB VRAM)

With 48GB VRAM, all top models fit simultaneously. Ranked by recommendation:

### Tier 1 — Deploy Both

#### Kokoro v1.0 ⭐ Primary (default)
| Property | Value |
|---|---|
| Quality | 9/10 — #1 ranked open-weight on TTS Arena |
| Speed | 35–100x real-time on GPU — fastest available |
| VRAM | ~2GB |
| Voice cloning | No — 54 pre-built voices across 8 languages |
| License | **Apache 2.0** (fully commercial) |
| Parameters | 82M |
| API | [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) — OpenAI-compatible |

Best default: fast, high quality, zero setup friction.

#### F5-TTS ⭐ Secondary (if voice cloning wanted)
| Property | Value |
|---|---|
| Quality | 9/10 — best zero-shot voice cloning available |
| Speed | 7–14x real-time (RTF 0.15) |
| VRAM | ~6–12GB |
| Voice cloning | **Yes — zero-shot, 10 seconds of reference audio** |
| License | MIT (code) + CC-BY-NC (base models) → Apache 2.0 variant available |
| API | [F5-TTS-Server](https://github.com/ValyrianTech/F5-TTS_server) or [F5TTS-FASTAPI](https://github.com/peytontolbert/F5TTS-FASTAPI) |

Use if a consistent, cloned voice identity for Hive Mind is desired.

### Tier 2 — Nice to Have

#### Chatterbox Turbo
- MIT licensed, ~4.5GB VRAM
- Unique: **emotion control tags** ([laugh], [cough], [chuckle])
- Few-shot voice cloning, 23 languages
- ELO 1,050, beats ElevenLabs in blind tests
- [Chatterbox-TTS-Server](https://github.com/devnen/Chatterbox-TTS-Server) — OpenAI-compatible API

#### MetaVoice-1B
- Apache 2.0, 1.2B params, ~12GB VRAM
- Zero-shot cloning from 30s reference audio
- Excellent American/British English expressiveness
- Has built-in FastAPI server

### Not Recommended
- **Fish Speech 1.5** — CC-BY-NC-SA (non-commercial only)
- **Spark-TTS** — CC-BY-NC-SA (non-commercial only)
- **Parler TTS** — Audiobook-optimised, poor conversational quality

---

## Suggested TTS Gateway

Since all models fit in 48GB simultaneously, run a single local TTS service with
multiple endpoints. Hive Mind calls one endpoint; swap default without redeploying bot.

```
POST /tts/kokoro       → Kokoro (fast default)
POST /tts/f5           → F5-TTS (voice clone)
POST /tts/chatterbox   → Chatterbox (emotion)
```

Service lives in `tts_server.py` alongside the other services, added to `docker-compose.yml`
with GPU passthrough (`deploy.resources.reservations.devices`).

---

## Open Questions

- **Voice cloning?** If yes, F5-TTS becomes the primary. Need a 10-second reference
  audio clip to define Hive Mind's voice identity.
- **Telegram bot token** — create via @BotFather, add `TELEGRAM_BOT_TOKEN` to `.env`
- **Allowed users** — same allowlist pattern as Discord, add `TELEGRAM_ALLOWED_USERS`
  to `config.yaml`

---

## Implementation Order

1. `tts_server.py` — FastAPI wrapper around Kokoro (start simple, add models later)
2. `telegram_bot.py` — text-only first, validate gateway integration
3. Add voice pipeline (faster-whisper STT + TTS call) to Telegram bot
4. Add `telegram-bot` service to `docker-compose.yml` with GPU passthrough
5. Update `requirements.txt` and `Dockerfile`

---

## New Dependencies

```
# requirements.txt additions
python-telegram-bot>=21.0
faster-whisper
kokoro
pydub          # audio format conversion
```

```
# System dependency (Dockerfile)
RUN apt-get install -y ffmpeg
```

---

## docker-compose additions

```yaml
tts-server:
  build: .
  container_name: hive-mind-tts
  volumes:
    - .:/usr/src/app
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
  command: ["venv/bin/python3", "tts_server.py"]
  networks:
    - hivemind

telegram-bot:
  build: .
  container_name: hive-mind-telegram
  volumes:
    - .:/usr/src/app
    - ~/.claude:/home/hivemind/.claude
  env_file:
    - .env
  restart: unless-stopped
  depends_on:
    - server
    - tts-server
  networks:
    - hivemind
  environment:
    - HIVE_MIND_SERVER_URL=http://server:8420
    - TTS_SERVER_URL=http://tts-server:8421
  command: ["venv/bin/python3", "telegram_bot.py"]
```
