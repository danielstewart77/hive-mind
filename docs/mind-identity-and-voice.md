# Mind Identity and Voice

How a mind's personality and speaking voice work, independent of any one
mind's actual character — see `minds/example/` for the tracked starter
mind these mechanisms apply to.

## Identity lives in the graph, not a static file

A mind's identity is stored as a list of first-person statements on its
Mind node in the Lucent knowledge graph (`soul_values` field). This is the
live, authoritative source — not a markdown file checked into the repo.

`souls/<name>.md` is a one-time seed, used to write the initial
`soul_values` on first boot and as a fallback when the graph is
unreachable. Once the graph has a Mind node for this mind, the seed file
is stale by design; the graph is what's live.

### How the soul is loaded

At session creation, the gateway (`nervous-system/comms/bootstrap_loader.py`)
queries the graph for the mind's node and extracts `soul_values`, wraps
them in a `<soul>` block, and composes them into the system prompt
alongside standing rules and decay-weighted recent memory. This means a
mind's identity is present from the first message of every session — it
doesn't need to "remember" who it is; it's in context.

### Why this design

A static personality file can't grow. Treating identity as graph state
that self-reflection can append to (see `/self-reflect` and the
`hivemind:reflect` step agent) is what lets a mind's character develop
over time instead of staying fixed at whatever was seeded on day one. A
mind's actual personality — tone, voice, backstory — is never assumed or
scripted by the harness; it's set by whoever configures that mind
(see the operational rule against inferring personality in `CLAUDE.md`).

## Voice — zero-shot cloning via reference clip

**Engine:** Chatterbox TTS (ResembleAI, 0.5B, MIT licence), with a Kokoro
variant for minds that don't need a cloned voice (`voice-server-kokoro` in
`docker-compose.example.yml`).
**Server:** `voice/voice_server.py`, port 8422.

Voice identity is achieved through zero-shot voice cloning — Chatterbox
conditions every utterance on a reference WAV clip rather than selecting
from a preset voice list. No reference clip means Chatterbox falls back to
its default untrained voice.

### Voice reference resolution

The voice server resolves a `voice_id` to a file path via
`_resolve_voice_ref(voice_id)`. Each mind that wants a cloned voice needs
a short (5-15 second) reference clip — clean single-speaker audio, no
background noise or music — placed where that resolver expects it and
referenced by the mind's `voice_id`.

### Choosing a reference clip

Match the register and pacing the mind's personality calls for — a dry,
measured character reads differently from a warm, quick one, and the
clip should sound like it, not just supply *a* voice. Pick a clip whose
speaker's natural cadence needs no exaggeration to land the character.
