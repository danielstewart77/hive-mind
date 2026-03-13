# Chatterbox TTS Migration

## Status: Build failing — needs host-side fix (2026-03-12 ~19:34 CDT)

Migrating from Fish Speech (broken, see `fish.md`) to **Chatterbox TTS** by Resemble AI.

---

## What was changed

### `requirements.txt`
- Added `chatterbox-tts`
- Added `torchaudio`
- Removed `suno-bark` and `torch<2.6.0` pin (these conflicted with Chatterbox)

### `voice/voice_server.py`
- Added `chatterbox` as a TTS backend option (startup load, TTS endpoint, backend-switch endpoint)
- Default backend changed from `fish` → `chatterbox`
- Chatterbox uses `voice_ref/hive_mind_voice.wav` as reference audio (same file, passed as path not base64)
- Key code: `ChatterboxTTS.from_pretrained(device="cuda")` then `model.generate(text, audio_prompt_path=ref_wav)`

### `docker-compose.yml`
- Removed `fish-speech` service entirely
- Removed `depends_on: fish-speech` from voice-server
- Changed `TTS_BACKEND=fish` → `TTS_BACKEND=chatterbox`
- Removed `FISH_SPEECH_URL` and `FISH_REF_TEXT` env vars
- Kept `FISH_REF_AUDIO` env var (reused for Chatterbox reference audio path)

---

## Build command

```bash
docker compose -p hive_mind up -d --build voice-server
```

---

## What's failing

The build fails during `pip install`. Exact error unknown from here — Daniel said "failing still" after removing `suno-bark` and `torch<2.6.0`.

### Likely causes to check

1. **`chatterbox-tts` has conflicting deps** — check the build output. Look for version conflicts involving `torch`, `transformers`, `torchaudio`, or `huggingface-hub`.

2. **`pip-audit` hook** — the project has a `pip-audit` pre-commit/build hook that scans for vulnerabilities. If any new package has a known CVE, it'll fail. Try running the build with `--no-cache` to get full output:
   ```bash
   docker compose -p hive_mind build voice-server 2>&1 | tail -50
   ```

3. **`torchaudio` version pin conflict** — `torchaudio` must match the `torch` version. If `chatterbox-tts` pins a specific torch version that mismatches torchaudio, fail. Try removing `torchaudio` from `requirements.txt` (chatterbox-tts may install it itself).

4. **`torch` already installed at wrong version** — the Docker layer cache might have an old torch. Try:
   ```bash
   docker compose -p hive_mind build --no-cache voice-server
   ```

### If pip-audit is the blocker

Check if `pip-audit` is running as a build step in the Dockerfile. If so, you may need to audit separately or exclude the new packages temporarily.

---

## What Chatterbox needs

- `pip install chatterbox-tts` — installs the model code
- On first startup, `ChatterboxTTS.from_pretrained(device="cuda")` downloads model weights (~1-2GB) to HuggingFace cache (`/home/hivemind/.cache/huggingface/`)
- The `whisper-cache` volume is mounted at `/home/hivemind/.cache` in the voice-server container — weights will persist there across restarts
- GPU: expects CUDA. ~2-3GB VRAM. Much lighter than Fish S2-Pro (22GB).

---

## Dockerfile location

`/usr/src/app/Dockerfile` — the voice-server shares the same image as the main server. The pip install from `requirements.txt` happens during build.

---

## Verification after successful build

```bash
# Check logs for Chatterbox loading
docker compose -p hive_mind logs voice-server --tail=20

# Expected startup line:
# INFO:hive-mind.voice:Loading Chatterbox TTS (ResembleAI/chatterbox)...
# INFO:hive-mind.voice:Voice server ready. TTS: Chatterbox | ref: /usr/src/app/voice_ref/hive_mind_voice.wav

# Test TTS directly
docker exec hive-mind-voice curl -s -X POST http://localhost:8422/tts \
  -H 'Content-Type: application/json' \
  -d '{"text": "Hello, this is a test of Chatterbox."}' \
  -o /usr/src/app/voice/test_chatterbox.ogg -w '%{http_code}'
# Should return 200 and write a valid OGG file
```

---

## Reference audio

- Path: `voice_ref/hive_mind_voice.wav` — 10s Joanna Lumley clip, 44.1kHz mono
- Transcript: `voice_ref/hive_mind_voice.txt` (179 chars) — used for Fish Speech only, not Chatterbox
- Chatterbox only needs the WAV file (no transcript required for voice cloning)
