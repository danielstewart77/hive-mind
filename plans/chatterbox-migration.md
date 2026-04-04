# Chatterbox Voice Engine Migration

## User Requirements

Replace the current XTTS v2 (Coqui) TTS engine with Chatterbox (ResembleAI). Chatterbox was proven working on 2026-03-12 via CLI test but the changes were lost when the container was restarted before being committed. All dependency constraints from that session are documented and known. The migration must be fully committed and deployed — nothing ephemeral.

## User Acceptance Criteria

- [ ] Voice server starts cleanly; `/health` returns `"tts": "ready"` with `"tts_engine": "chatterbox"`
- [ ] `/tts` endpoint returns audible OGG voice message using Joanna Lumley reference clip
- [ ] Voice quality matches or exceeds the Mar 12 working Chatterbox demo
- [ ] Telegram test voice message sounds correct (not slow, not slurred, not robotic)
- [ ] `voice_id` parameter supported: `/tts` accepts `voice_id` that maps to `voice_ref/{voice_id}.wav`; defaults to `ada`
- [ ] `voice_ref/ada.wav` exists (renamed from `hive_mind_voice.wav`); `voice_ref/default.wav` symlinks to it
- [ ] Build-time import validation passes: `from chatterbox.tts import ChatterboxTTS; from faster_whisper import WhisperModel`
- [ ] All changed files committed and pushed to master before session ends
- [ ] `tts-models` named volume removed from docker-compose.yml (XTTS cache no longer needed)
- [ ] `specs/chatterbox.md` exists documenting synthesis code, init pattern, known gotchas

## Technical Specification

### Architecture

Single-engine voice server: Chatterbox handles all TTS, faster-whisper handles all STT. No fallback engine — if Chatterbox fails, the build fails (validated at build time).

**Synthesis flow:**
1. Load `ChatterboxTTS.from_pretrained(device=device)` at startup
2. On `/tts` request: resolve `voice_ref/{voice_id}.wav` (default: `ada`)
3. Call `model.generate(text, audio_prompt_path=ref_wav_path)` → tensor → numpy
4. Convert WAV → OGG via existing `_wav_to_ogg()` helper
5. Return OGG bytes

**STT flow:** unchanged — faster-whisper, `/stt` endpoint, OGG → WAV → transcription.

### Dependency Constraints (from Mar 12 session)

| Constraint | Reason |
|-----------|--------|
| `setuptools<81` | `resemble-perth` (Chatterbox dep) uses `pkg_resources`, removed in setuptools 82+ |
| `chatterbox-tts` installed `--no-deps` | It pins `numpy<1.26`, incompatible with Python 3.12 wheels — install deps separately |
| `torch==2.6.0` | Chatterbox's pinned version |
| `transformers==4.46.3` | Chatterbox-compatible pin |
| Python 3.11-slim base | Keep as-is; 3.12 is fine but 3.11 already works |

### Voice Registry

```
voice_ref/
  ada.wav        # Ada's voice — Joanna Lumley reference clip (~10s)
  default.wav    # Symlink → ada.wav (fallback for unrecognised voice_id)
```

Each bot passes `voice_id` in the TTS request body. Voice server resolves the file path — no model reload needed per request (Chatterbox accepts `audio_prompt_path` per call).

### Environment Variables

| Var | Default | Purpose |
|-----|---------|---------|
| `VOICE_REF_DIR` | `/usr/src/app/voice_ref` | Directory containing voice WAV files |
| `WHISPER_MODEL` | `medium` | Faster-whisper model size |

Remove: `XTTS_REF_AUDIO`, `XTTS_LANGUAGE`, `COQUI_TOS_AGREED`

### Named Volumes

Remove `tts-models` (was XTTS cache at `~/.local/share/tts/`). Chatterbox caches to `~/.cache/huggingface/` — already covered by the existing `whisper-cache` volume.

## Code References

| File | Change |
|------|--------|
| `voice/voice_server.py` | Replace XTTS synthesis with Chatterbox; add `voice_id` to TTSRequest |
| `requirements.voice.txt` | Swap Coqui/XTTS deps for Chatterbox deps |
| `Dockerfile.voice` | Add `setuptools<81`, `--no-deps chatterbox-tts`, update build validation |
| `docker-compose.yml` | Remove `tts-models` volume, swap env vars |
| `voice_ref/hive_mind_voice.wav` | Rename → `voice_ref/ada.wav` |
| `voice_ref/default.wav` | Create symlink → `ada.wav` |
| `specs/chatterbox.md` | New — working synthesis code reference |
| `specs/containers.md` | Update migration plan status to complete |

## Implementation Order

1. **Extract baseline from git** — `git show ea5d83a:voice/voice_server.py` for Chatterbox `_synthesize()` and startup code
2. **Create `specs/chatterbox.md`** — document synthesis pattern, init call, known gotchas
3. **Update `requirements.voice.txt`** — remove XTTS deps, add Chatterbox deps per constraints table
4. **Update `Dockerfile.voice`** — add `setuptools<81` to pip setup, add `--no-deps chatterbox-tts` install, update build validation import
5. **Rewrite `voice/voice_server.py`** — Chatterbox engine, `voice_id` parameter, keep all endpoints and STT unchanged
6. **Update `docker-compose.yml`** — remove `tts-models` volume, swap env vars
7. **Rename voice reference** — `voice_ref/hive_mind_voice.wav` → `voice_ref/ada.wav`, create `default.wav` symlink
8. **Build** — `docker compose up -d --build voice-server`
9. **Verify** — check logs for "Voice server ready. TTS: Chatterbox", send Telegram test message
10. **Commit and push** — ALL changed files; do not end session without this step
