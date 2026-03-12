# Fish Speech Setup — COMPLETE

## Status: Working (2026-03-12)
Fish Speech s2-pro is running as the primary TTS backend. Tested and producing audio.

## What happened
1. Original `fish-speech-1.5` weights were downloaded into wrong Docker volume (`hivemindfish-speech-checkpoints` vs `hive_mind_fish-speech-checkpoints`) due to Telegram stripping underscores.
2. The Docker image (`fishaudio/fish-speech:server-cuda`) was updated to expect the newer **s2-pro** model, not fish-speech-1.5. Different architecture: safetensors format, HuggingFace tokenizer, `codec.pth` decoder (~10.3GB total vs 1.4GB).
3. Downloaded `fishaudio/s2-pro` from HuggingFace into the correct volume. Model loads successfully, uses 22GB VRAM.

## Architecture
- `fish-speech` container: `fishaudio/fish-speech:server-cuda`, listens on port 8080 (internal only)
- `voice-server` container: routes TTS requests to fish-speech via `TTS_BACKEND=fish` env var
- `voice_server.py` passes reference audio (`voice_ref/hive_mind_voice.wav`) as base64 in every request for voice cloning
- Switch backends live: `POST http://voice-server:8422/backend` with `{"backend": "fish"}` or `{"backend": "bark"}`

## Useful commands
```bash
# Check fish-speech logs
docker compose -p hive_mind logs fish-speech --tail=30

# Health check (from inside network — port not exposed to host)
docker exec hive-mind-voice curl -s http://localhost:8422/health

# Test TTS
docker exec hive-mind-voice curl -s -X POST http://localhost:8422/tts \
  -H 'Content-Type: application/json' \
  -d '{"text": "Hello, this is a test."}' -o /tmp/test.ogg -w '%{http_code}'

# Switch back to Bark if needed
docker exec hive-mind-voice curl -s -X POST http://localhost:8422/backend \
  -H 'Content-Type: application/json' -d '{"backend": "bark"}'
```

## Cleanup
Stale volume from the original bad download:
```bash
docker volume rm hivemindfish-speech-checkpoints
```
