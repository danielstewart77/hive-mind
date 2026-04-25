# Hive Mind — Voice Identity

## Ada's Voice Character

**Female. Dry, measured, wry.** Not warm and eager — competent and direct.
The "dry, occasionally wry" character reads more distinctively in a female voice and cuts against
the stereotypical over-eager AI assistant tone.

Register: **contralto or lower mezzo** — not artificially deep, but not high or chirpy.
Pacing: deliberate, not rushed. Wit comes through cadence, not exaggerated inflection.
Warmth: present but not performed. Sounds like it means what it says.

Ada's reference clip is a 10-second clip of Joanna Lumley speaking — naturally embodies the dry,
measured British female register Ada aims for.

---

## Current Implementation — Chatterbox TTS

**Engine:** Chatterbox TTS (ResembleAI, 0.5B, MIT licence)
**Server:** `voice/voice_server.py`, port 8422
**Entry point:** `Dockerfile.voice`

Voice identity is achieved through **zero-shot voice cloning** — Chatterbox conditions every
utterance on a reference WAV clip rather than selecting from a preset voice list. No reference
clip means Chatterbox falls back to its default untrained voice.

### Voice Reference Resolution

The voice server resolves a `voice_id` to a file path via `_resolve_voice_ref(voice_id)`:

```
minds/{voice_id}/voice_ref.wav
```

For example, Ada's reference is at `minds/ada/voice_ref.wav` inside the container. The voice
server container mounts the full hive_mind project directory at `/usr/src/app`, so it has
read access to all `minds/*/voice_ref.wav` files automatically — no extra mounts needed for
minds that live inside hive_mind.

If `voice_ref.wav` is not found for the requested `voice_id`, the function returns `None` and
Chatterbox synthesises using its default voice (no cloning).

### TTS Request

```http
POST http://voice-server:8422/tts
Content-Type: application/json

{
  "text": "Hello.",
  "voice_id": "ada",
  "speed": 0.9
}
```

Response: `audio/ogg` (Opus-encoded, ready for Telegram voice notes).

### Performance

- ~2–3GB VRAM
- EOS detection at ~60–210 steps
- ~63 tokens/sec on RTX A6000

---

## Mind Folder Convention

Every mind that wants a cloned voice needs **one file** inside hive_mind:

```
minds/{mind_id}/voice_ref.wav
```

For minds that live entirely within hive_mind (Ada, Bob, Bilby, Nagatha), this file lives
alongside their `implementation.py`, `.claude/`, etc.

For **external/standalone minds** that share the voice server but run outside hive_mind,
the convention is the same: add a minimal `minds/{mind_id}/` folder to hive_mind containing
only `voice_ref.wav`. No `.claude/`, no implementation, no other files — just the reference
clip. This is the correct approach rather than inventing a separate lookup path.

Example for Skippy (standalone mind, shared voice server):

```
hive_mind/
└── minds/
    └── skippy/
        └── voice_ref.wav   ← only file needed
```

Skippy's actual implementation, config, and data live in its own separate project directory.

---

## Adding or Replacing a Voice Reference

1. Obtain a clean 10-second mono WAV clip (16kHz recommended; Chatterbox resamples if needed).
2. Place it at `minds/{mind_id}/voice_ref.wav` inside the hive_mind project directory.
3. No server restart needed — `_resolve_voice_ref` reads the file at synthesis time.

---

## Accessing the Voice Server

**From within the hivemind Docker network (hive_mind minds):**
```
http://voice-server:8422
```

**From external minds or the host:**
```
http://{host_ip}:8422
```

Port 8422 is bound to the host in `docker-compose.yml` (`ports: - "8422:8422"`), making it
reachable from any process on the host or the local network. External minds should set
`VOICE_SERVER_URL=http://{host_ip}:8422` in their environment.

---

## Fallback — Bark

**Engine:** Bark (suno-ai, neural)
**Activated by:** `POST /backend {"backend": "bark"}`

Bark uses a preset voice (`v2/en_speaker_6`) and does not support voice cloning.
Lower quality than Chatterbox but fully local and does not depend on a reference clip.

---

## Health Check

```
GET http://voice-server:8422/health
```

Returns STT/TTS readiness, engine in use, and device (cuda/cpu).
