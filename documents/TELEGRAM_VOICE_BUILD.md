# Telegram Voice Bot — Build Guide

**Status:** Ready to build (pending: Telegram token, F5-TTS reference audio clip)
**See also:** `TELEGRAM_VOICE_PLAN.md` for architecture decisions and TTS model research

---

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Messaging platform | Telegram | First-class voice notes, simple bot API, all platforms |
| STT | faster-whisper large-v3 | Best accuracy, Apache 2.0, ~3GB VRAM, 10-20x RT on A6000 |
| TTS primary | F5-TTS | Zero-shot voice cloning — Hive Mind gets a consistent voice identity |
| TTS fallback | Kokoro v1.0 | 35-100x RT, Apache 2.0, used when speed matters more than cloning |
| Voice cloning | Yes | 10-second reference audio clip needed (user to provide) |
| Token storage | `.env` (same as Discord) | Already gitignored, same risk profile |

---

## Prerequisites

Before building:
1. Create Telegram bot via @BotFather → get `TELEGRAM_BOT_TOKEN`
2. Record 10-second clean voice clip (WAV/MP3) for F5-TTS voice cloning → save as `voice_ref/hive_mind_voice.wav`
3. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS` to `.env`
4. RTX A6000 with CUDA drivers + `nvidia-container-toolkit` installed on host

---

## New Files

```
hive_mind/
├── telegram_bot.py          # Telegram thin client (mirrors discord_bot.py)
├── tts_server.py            # FastAPI TTS service (F5-TTS + Kokoro)
├── voice_ref/
│   └── hive_mind_voice.wav  # Reference audio for F5-TTS voice cloning
└── documents/
    └── TELEGRAM_VOICE_BUILD.md  # this file
```

---

## Modified Files

```
requirements.txt     # add: python-telegram-bot, faster-whisper, f5-tts, kokoro, pydub
Dockerfile           # add: ffmpeg apt package, CUDA base image consideration
docker-compose.yml   # add: tts-server and telegram-bot services
config.yaml          # add: telegram_allowed_users, tts_server_url
config.py            # add: telegram config fields
```

---

## Implementation Steps

### Step 1 — `tts_server.py`

FastAPI service exposing two endpoints. Loads both models at startup (fits in 48GB).

```
POST /tts/f5
  Body: { "text": str, "speed": float = 1.0 }
  Returns: audio/ogg bytes (Opus, Telegram-compatible)

POST /tts/kokoro
  Body: { "text": str, "voice": str = "af_heart", "speed": float = 1.0 }
  Returns: audio/ogg bytes

GET /health
  Returns: { "f5": "ready", "kokoro": "ready" }
```

**F5-TTS setup:**
- Load model once at startup
- Reference audio: `voice_ref/hive_mind_voice.wav`
- Generate WAV → convert to OGG/Opus via ffmpeg subprocess
- All inference on GPU (`device="cuda"`)

**Kokoro setup:**
- Load pipeline once at startup (`from kokoro import KPipeline`)
- Voice: `af_heart` (warm, clear American English female) or user preference
- Generate samples → stitch → convert to OGG/Opus

**Audio conversion (both models):**
```python
# WAV bytes → OGG/Opus bytes
ffmpeg -i pipe:0 -c:a libopus -b:a 64k -f ogg pipe:1
```
Telegram voice notes require OGG container with Opus codec.

---

### Step 2 — `telegram_bot.py` (text only first)

Mirror `discord_bot.py` structure exactly:

```python
# Key differences from discord_bot.py:
# - python-telegram-bot library instead of discord.py
# - Session keyed on chat_id (telegram's equivalent of channel_id)
# - Allowed users from TELEGRAM_ALLOWED_USERS env var
# - No slash command tree sync needed (Telegram handles differently)
# - Polling mode or webhook (polling is simpler to start)
```

**Allowed users config** — add to `config.yaml`:
```yaml
telegram_allowed_users: [123456789]  # Telegram user IDs (integers)
```

**Session flow** (identical to Discord):
```python
async def handle_message(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id not in config.telegram_allowed_users:
        return
    text = update.message.text
    response = await _query(text, user_id, chat_id)
    await update.message.reply_text(response)
```

**Gateway calls** — reuse exact same `_ensure_session`, `_query`, `_server_command`
logic as `discord_bot.py`. Only the send/receive wrappers change.

---

### Step 3 — Voice pipeline in `telegram_bot.py`

```python
async def handle_voice(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id not in config.telegram_allowed_users:
        return

    # 1. Download OGG voice note from Telegram
    voice_file = await update.message.voice.get_file()
    ogg_bytes = await voice_file.download_as_bytearray()

    # 2. Convert OGG → WAV (ffmpeg)
    wav_bytes = ogg_to_wav(ogg_bytes)

    # 3. Transcribe WAV → text (faster-whisper)
    text = transcribe(wav_bytes)  # runs locally on GPU

    # 4. Send to gateway, get text response
    response = await _query(text, user_id, chat_id)

    # 5. Synthesise response → OGG (TTS server)
    audio = await tts(response)  # POST http://tts-server:8421/tts/f5

    # 6. Send voice note back
    await update.message.reply_voice(voice=audio)
```

**faster-whisper initialisation** (once at startup):
```python
from faster_whisper import WhisperModel
_whisper = WhisperModel("large-v3", device="cuda", compute_type="float16")

def transcribe(wav_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(wav_bytes)
        segments, _ = _whisper.transcribe(f.name, language="en")
    return " ".join(s.text for s in segments).strip()
```

---

### Step 4 — `docker-compose.yml` additions

```yaml
tts-server:
  build: .
  container_name: hive-mind-tts
  working_dir: /usr/src/app
  volumes:
    - .:/usr/src/app
  restart: unless-stopped
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
  networks:
    - hivemind
  command: ["venv/bin/python3", "tts_server.py"]

telegram-bot:
  build: .
  container_name: hive-mind-telegram
  working_dir: /usr/src/app
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

**Note on GPU sharing:** The `tts-server` gets the GPU. `telegram_bot.py` loads
faster-whisper which also needs CUDA. Two options:
- **Option A:** Move STT into `tts_server.py` as a `/stt` endpoint — one container
  owns the GPU, clean separation
- **Option B:** Both containers share the GPU (works fine on 48GB, just set
  `CUDA_VISIBLE_DEVICES=0` on both)

**Recommendation: Option A** — cleaner, `telegram_bot.py` stays CPU-only/thin.

If using Option A, add to `tts_server.py`:
```
POST /stt
  Body: audio bytes (WAV)
  Returns: { "text": str }
```

---

### Step 5 — `Dockerfile` changes

```dockerfile
# Add ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# CUDA base: if not already using CUDA image, switch base to:
FROM nvidia/cuda:12.3.0-runtime-ubuntu22.04
# (check current Dockerfile base first)
```

---

### Step 6 — `requirements.txt` additions

```
python-telegram-bot[job-queue]>=21.0
faster-whisper>=1.0.0
f5-tts
kokoro>=0.9.0
pydub
soundfile
```

---

### Step 7 — `config.yaml` additions

```yaml
telegram_allowed_users: []   # fill with your Telegram user ID
tts_server_url: http://tts-server:8421
tts_model: f5                # "f5" or "kokoro"
```

---

### Step 8 — `.env` additions

```ini
TELEGRAM_BOT_TOKEN=your_token_here
```

---

## Voice Reference Audio

F5-TTS needs a 10-second WAV clip of the voice to clone. Guidelines:
- Clean recording, no background noise
- Single speaker, natural speech
- 10–30 seconds optimal (longer = more accurate cloning)
- WAV format, 22050Hz or 44100Hz sample rate
- Save to: `voice_ref/hive_mind_voice.wav`

To give Hive Mind its own distinct voice: record or find a 10-second clip of any
voice you want the assistant to use. This is stored locally, never leaves the machine.

---

## Testing Order

1. `docker compose up tts-server` → `curl -X POST http://localhost:8421/health`
2. Send text to `/tts/kokoro` endpoint, verify OGG audio returned
3. Send text to `/tts/f5` endpoint, verify cloned voice audio returned
4. `docker compose up telegram-bot` → send text message to bot in Telegram
5. Send voice note to bot → verify transcription + voice response

---

## VRAM Budget (RTX A6000, 48GB)

| Component | VRAM |
|---|---|
| faster-whisper large-v3 | ~3 GB |
| F5-TTS | ~8 GB |
| Kokoro v1.0 | ~2 GB |
| **Total** | **~13 GB** |
| **Remaining** | **~35 GB free** |

Ample headroom. All models load at startup, no swapping needed.
