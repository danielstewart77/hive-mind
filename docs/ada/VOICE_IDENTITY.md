# Hive Mind — Voice Identity

## Decision

**Female. Dry, measured, wry.** Not warm and eager — competent and direct.
The "dry, occasionally wry" character reads more distinctively in a female voice and cuts against
the stereotypical over-eager AI assistant tone.

Register: **contralto or lower mezzo** — not artificially deep, but not high or chirpy.
Pacing: deliberate, not rushed. Wit comes through cadence, not exaggerated inflection.
Warmth: present but not performed. Sounds like it means what it says.

---

## Current Implementation — Kokoro v1.0

**Engine:** Kokoro v1.0 (`kokoro` Python package)
**Server:** `voice_server.py` on port 8422
**Config var:** `KOKORO_VOICE` in docker-compose / environment

The server currently uses `lang_code="a"` (American English), which limits available voices to
American Female (`af_*`) and American Male (`am_*`) presets.

### Best current option

| Voice | Description | Fit |
|-------|-------------|-----|
| `af_bella` | American Female — authoritative, clear | **Best current match** |
| `af_heart` | American Female — warm, natural | Current default; slightly too warm |
| `af_sarah` | American Female — professional, even | Acceptable fallback |
| `am_adam` | American Male — neutral | Not preferred |

**Recommendation:** Switch default to `af_bella`.

### Near-term improvement (no GPU needed)

Kokoro supports British English voices by changing `lang_code="b"`. These carry the dry,
wry quality more naturally and would be a better fit for the persona.

| Voice | Description | Fit |
|-------|-------------|-----|
| `bf_alice` | British Female — clear, measured | Strong candidate |
| `bf_emma` | British Female — warm British | Good fallback |
| `bm_daniel` | British Male — authoritative | If ever reconsidering male |

To enable: update `voice_server.py` to support `lang_code="b"` (or dynamic per-voice routing),
and set `KOKORO_VOICE=bf_alice`.

---

## Future Upgrade — F5-TTS (pending A6000 GPU)

**Engine:** F5-TTS (zero-shot voice cloning)
**Reference file:** `voice_ref/hive_mind_voice.wav` (10-second reference clip needed)
**Planned port:** 8421 (`tts_server.py` — separate from current voice server)
**Kokoro** remains as a fast fallback.

With F5-TTS, voice identity becomes a recorded reference clip rather than a preset selection,
enabling a fully custom voice that matches this persona exactly.

See `documents/TELEGRAM_VOICE_BUILD.md` for implementation details.

---

## Speed

Default speed (`1.0`) is fine. Slightly faster (`1.05–1.1`) could suit the direct cadence
without sounding rushed — worth testing when switching voices.
