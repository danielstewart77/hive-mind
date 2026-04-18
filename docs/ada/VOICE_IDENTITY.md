# Hive Mind — Voice Identity

## Decision

**Female. Dry, measured, wry.** Not warm and eager — competent and direct.
The "dry, occasionally wry" character reads more distinctively in a female voice and cuts against
the stereotypical over-eager AI assistant tone.

Register: **contralto or lower mezzo** — not artificially deep, but not high or chirpy.
Pacing: deliberate, not rushed. Wit comes through cadence, not exaggerated inflection.
Warmth: present but not performed. Sounds like it means what it says.

---

## Current Implementation — Chatterbox TTS

**Engine:** Chatterbox TTS (ResembleAI, 0.5B, MIT licence)
**Server:** `voice_server.py` on port 8422
**Config var:** `TTS_BACKEND=chatterbox` in docker-compose environment

Voice identity is achieved through **zero-shot voice cloning** — Chatterbox conditions every
utterance on a 10-second reference clip rather than selecting from a preset voice list.

**Reference audio:** `voice_ref/{voice_id}.wav` (default: `voice_ref/default.wav`)
The `voice_id` parameter on the TTS endpoint selects the reference clip. Falls back to
`default.wav` if the requested file doesn't exist.

Ada's reference clip is `voice_ref/ada.wav` — a 10-second clip of Joanna Lumley speaking.
Chosen because it naturally embodies the dry, measured British female register Ada aims for.

**Reference transcript:** `voice_ref/hive_mind_voice.txt`
"I've loved it since I was a child ffff for the reasons that, what, I don't think my parents
read me poetry, but I had a kind of a feeling I liked the sound of the pattern it made."

### Performance

- ~2–3GB VRAM (vs 22GB for Fish Speech S2-Pro)
- EOS detection at ~60–210 steps (fast)
- Generation speed: ~63 tokens/sec on RTX A6000

### Updating the voice

To change Ada's reference voice, replace `voice_ref/ada.wav` with a new 10-second clean mono WAV.
To add a voice for another mind, add `voice_ref/{mind_id}.wav`. Reload via:
```bash
curl -X POST http://voice-server:8422/backend \
  -H 'Content-Type: application/json' \
  -d '{"backend": "chatterbox"}'
```

---

## Fallback — Bark

**Engine:** Bark (suno-ai, neural)
**Activated by:** `POST /backend {"backend": "bark"}`

Bark uses a preset voice (`v2/en_speaker_6`) and does not support voice cloning.
Lower quality than Chatterbox but fully local and does not depend on a reference clip.

---

## Speed

Default speed (`1.0`) is fine. Slightly faster (`1.05–1.1`) could suit the direct cadence
without sounding rushed — worth testing if latency allows.
