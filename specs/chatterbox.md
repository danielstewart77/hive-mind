# Chatterbox TTS Reference

Working synthesis code reference for the Chatterbox TTS engine (ResembleAI).

## Synthesis Pattern

```python
from chatterbox.tts import ChatterboxTTS

# Load model at startup (once)
model = ChatterboxTTS.from_pretrained(device=device)

# Generate speech (per request)
wav_tensor = model.generate(text, audio_prompt_path=ref_wav_path)

# Save to WAV buffer
import torchaudio, io
buf = io.BytesIO()
torchaudio.save(buf, wav_tensor, model.sr, format="WAV")
wav_bytes = buf.getvalue()
```

## Init Pattern

```python
_chatterbox_model = None

@app.on_event("startup")
async def startup():
    global _chatterbox_model
    from chatterbox.tts import ChatterboxTTS
    _chatterbox_model = ChatterboxTTS.from_pretrained(device=_DEVICE)
```

## Dependency Constraints

| Package | Constraint | Reason |
|---------|-----------|--------|
| `setuptools` | `<81` | `resemble-perth` uses `pkg_resources`, removed in setuptools 82+ |
| `chatterbox-tts` | `--no-deps` | Pins `numpy<1.26`, incompatible with relaxed numpy |
| `torch` | `==2.6.0` | Chatterbox pinned version |
| `torchaudio` | `==2.6.0` | Must match torch version |
| `transformers` | `==4.46.3` | Chatterbox-compatible pin |

## Known Gotchas

- **numpy pin**: `chatterbox-tts` pins `numpy<1.26` in its metadata. Install with `--no-deps` and provide numpy separately without the upper bound.
- **pkg_resources**: `resemble-perth` imports `pkg_resources` at runtime. This module was removed from `setuptools>=82`. Pin `setuptools<81` in the Docker venv setup.
- **Model cache path**: Chatterbox downloads models to `~/.cache/huggingface/`. The `whisper-cache` Docker volume at `/home/hivemind/.cache` covers both Whisper and Chatterbox models.
- **Sample rate**: Access via `model.sr` (not hardcoded). Currently 24000 Hz.

## Voice Reference Format

- WAV format, mono or stereo
- ~10 seconds of clear speech
- No transcript needed (Chatterbox is WAV-only, zero-shot cloning)
- Files stored in `voice_ref/` directory, named `{voice_id}.wav`
