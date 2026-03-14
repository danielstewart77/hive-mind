# XTTS v2 Voice Engine

## User Requirements

Replace the current F5-TTS + Kokoro TTS stack with XTTS v2 (Coqui). The primary driver is name/proper noun pronunciation — Kokoro's phonemizer silently skips names it can't resolve. XTTS v2 handles this correctly and supports voice cloning from a reference audio clip, preserving Ada's cloned voice capability.

## User Acceptance Criteria

- [ ] Voice server uses XTTS v2 for all TTS synthesis
- [ ] Proper nouns and names are pronounced (not skipped)
- [ ] Voice cloning from `voice_ref/hive_mind_voice.wav` is active by default
- [ ] Falls back to a stock XTTS voice if reference audio is missing
- [ ] `/speak` endpoint contract is unchanged (same request/response shape)
- [ ] Runs on A6000 GPU (48GB VRAM); no OOM errors
- [ ] Voice server starts cleanly; `/health` returns ready state

## Technical Specification

### Architecture

Replace two engines (F5-TTS + Kokoro) with one: `TTS` (Coqui), model `tts_models/multilingual/multi-dataset/xtts_v2`.

**Synthesis flow:**
1. Load XTTS v2 model at startup with GPU
2. On `/speak` request: run `tts.tts_with_vc()` with reference WAV for voice cloning
3. Convert WAV output → OGG/Opus (existing `_wav_to_ogg()` unchanged)
4. Return OGG bytes

**Voice cloning:** `tts.tts_with_vc(text, speaker_wav=REF_AUDIO, language="en")`

**Fallback:** if `REF_AUDIO` missing or invalid, use `tts.tts(text, speaker="Claribel Dervla")` (built-in XTTS speaker)

### Environment Variables

| Var | Default | Purpose |
|-----|---------|---------|
| `XTTS_REF_AUDIO` | `/usr/src/app/voice_ref/hive_mind_voice.wav` | Reference clip for voice cloning |
| `XTTS_LANGUAGE` | `en` | Synthesis language |

Remove: `KOKORO_VOICE`, `F5_REF_AUDIO`, `F5_REF_TEXT`

### Dependencies

Remove: `kokoro`, `f5-tts`, `vocos`
Add: `TTS` (Coqui — installs `tts` CLI + Python API)

Model is downloaded on first run (~2GB), cached in Docker volume or model cache dir.

## Code References

| File | Change |
|------|--------|
| `voice/voice_server.py` | Full rewrite of TTS logic — remove F5/Kokoro, add XTTS v2 |
| `voice/requirements.txt` | Swap dependencies |
| `docker-compose.yml` | Replace `KOKORO_VOICE`/`F5_*` env vars with `XTTS_*` |

## Implementation Order

1. Update `voice/requirements.txt` — remove old deps, add `TTS`
2. Rewrite `voice/voice_server.py` — XTTS init, `/speak` handler, fallback logic
3. Update `docker-compose.yml` — env vars
4. Rebuild voice-server container, test with a name-heavy phrase
5. Confirm `/health` shows ready and OGG output plays correctly
