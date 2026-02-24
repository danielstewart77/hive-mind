# Telegram Voice Bot — Build Guide (v0)

**Hardware:** Current GPU, 12GB VRAM
**Voice cloning:** No — Kokoro pre-built voices
**Upgrade path:** See `TELEGRAM_VOICE_BUILD.md` (A6000 + F5-TTS voice cloning)

---

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Messaging platform | Telegram | First-class voice notes, simple bot API, all platforms |
| STT | faster-whisper `medium` | ~1.5GB VRAM, excellent accuracy, safe on 12GB |
| TTS | Kokoro v1.0 | ~2GB VRAM, Apache 2.0, #1 ranked open-weight, 54 voices |
| Voice cloning | No | Insufficient VRAM headroom; deferred to A6000 |
| STT + TTS hosting | Single `voice_server.py` | One CUDA context = less overhead on 12GB |

**VRAM budget:**

| Component | VRAM |
|---|---|
| faster-whisper medium | ~1.5 GB |
| Kokoro v1.0 | ~2.0 GB |
| PyTorch / CUDA overhead | ~1.5 GB |
| **Total** | **~5 GB** |
| **Remaining** | **~7 GB free** |

---

## New Files

```
hive_mind/
├── telegram_bot.py      # Telegram thin client
└── voice_server.py      # Combined STT + TTS FastAPI service
```

---

## Modified Files

```
requirements.txt     # add: python-telegram-bot, faster-whisper, kokoro, pydub
Dockerfile           # add: ffmpeg
docker-compose.yml   # add: voice-server, telegram-bot services
config.yaml          # add: telegram_allowed_users
config.py            # add: telegram config fields
```

---

## Implementation Steps

### Step 1 — `voice_server.py`

Single FastAPI service handling both STT and TTS. Loads both models at startup.

**Endpoints:**

```
POST /stt
  Body: audio bytes (WAV, multipart or raw)
  Returns: { "text": str }

POST /tts
  Body: { "text": str, "voice": str = "af_heart", "speed": float = 1.0 }
  Returns: audio/ogg bytes (Opus, Telegram-compatible)

GET /health
  Returns: { "stt": "ready", "tts": "ready" }
```

**Model init (at startup):**
```python
from faster_whisper import WhisperModel
from kokoro import KPipeline
import soundfile as sf

_whisper = WhisperModel("medium", device="cuda", compute_type="float16")
_kokoro = KPipeline(lang_code="a")  # "a" = American English
```

**STT handler:**
```python
# Receive WAV bytes → transcribe → return text
segments, _ = _whisper.transcribe(wav_path, language="en")
text = " ".join(s.text for s in segments).strip()
```

**TTS handler:**
```python
# Generate audio with Kokoro → convert WAV → OGG/Opus → return bytes
generator = _kokoro(text, voice=voice, speed=speed)
# stitch audio samples → write WAV → ffmpeg WAV→OGG
```

**Audio conversion (WAV → OGG/Opus for Telegram):**
```
ffmpeg -i input.wav -c:a libopus -b:a 64k -f ogg output.ogg
```

**Kokoro voice options (good defaults):**
- `af_heart` — warm American English female (recommended)
- `am_adam` — American English male
- `bf_emma` — British English female

---

### Step 2 — `telegram_bot.py`

Thin client mirroring `discord_bot.py`. Text flow is identical to Discord.

**Text message handler:**
```python
async def handle_text(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id not in config.telegram_allowed_users:
        return
    response = await _query(update.message.text, user_id, chat_id)
    await update.message.reply_text(response)
```

**Voice message handler:**
```python
async def handle_voice(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id not in config.telegram_allowed_users:
        return

    # 1. Download OGG from Telegram
    file = await update.message.voice.get_file()
    ogg_bytes = await file.download_as_bytearray()

    # 2. OGG → WAV (ffmpeg)
    wav_bytes = convert_ogg_to_wav(ogg_bytes)

    # 3. STT via voice_server
    text = await stt(wav_bytes)

    # 4. Query gateway
    response = await _query(text, user_id, chat_id)

    # 5. TTS via voice_server
    audio = await tts(response)

    # 6. Reply with voice note
    await update.message.reply_voice(voice=audio)
```

**Gateway calls** — reuse `_ensure_session` and `_query` logic from `discord_bot.py` verbatim.

**Startup:**
```python
app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))
app.run_polling()
```

---

### Step 3 — `docker-compose.yml` additions

```yaml
voice-server:
  build: .
  container_name: hive-mind-voice
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
  command: ["venv/bin/python3", "voice_server.py"]

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
    - voice-server
  networks:
    - hivemind
  environment:
    - HIVE_MIND_SERVER_URL=http://server:8420
    - VOICE_SERVER_URL=http://voice-server:8422
  command: ["venv/bin/python3", "telegram_bot.py"]
```

---

### Step 4 — `Dockerfile` addition

```dockerfile
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
```

---

### Step 5 — `requirements.txt` additions

```
python-telegram-bot[job-queue]>=21.0
faster-whisper>=1.0.0
kokoro>=0.9.0
pydub
soundfile
```

---

### Step 6 — `config.yaml` additions

```yaml
telegram_allowed_users: []   # your Telegram user ID (integer)
voice_server_url: http://voice-server:8422
```

---

### Step 7 — `.env` additions

```ini
TELEGRAM_BOT_TOKEN=your_token_here
```

---

## Testing Order

1. `docker compose up voice-server`
2. `curl http://localhost:8422/health` → verify both models ready
3. `curl -X POST http://localhost:8422/tts -d '{"text":"Hello"}' -o test.ogg` → play audio
4. `docker compose up telegram-bot`
5. Send text message to bot in Telegram → verify text reply
6. Send voice note → verify transcription + voice reply

---

## Upgrade Path to v1 (A6000)

When the A6000 arrives:
- Split `voice_server.py` into `tts_server.py` (TTS only, GPU) and move STT into it
- Upgrade STT model: `medium` → `large-v3`
- Add F5-TTS endpoint `/tts/f5` with voice cloning
- Add reference audio clip to `voice_ref/hive_mind_voice.wav`
- Update `docker-compose.yml` per `TELEGRAM_VOICE_BUILD.md`
