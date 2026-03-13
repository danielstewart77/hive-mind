# Fish Speech — `openaudio-s1-mini` broken, model doesn't generate speech

## Status: model fundamentally can't do TTS with current inference code (2026-03-12 ~18:55 CDT)

---

## Session 1 findings (Ada, ~18:10 CDT)

Initial symptom: Fish Speech generating garbage — model runs to 1023-token max and outputs noise. Happens with and without reference audio.

Hypothesis was that `voice/fish_tokenizer.py` patch (volume-mounted into container) was stale after a Docker image update.

---

## Session 2 findings (Claude, 18:10–18:55 CDT)

### What was tried

1. **Removed tokenizer patch entirely** — container's built-in tokenizer crashes on startup:
   ```
   UnboundLocalError: cannot access local variable 'tokenizer' where it is not associated with a value
   ```
   Root cause: `llama.py:523` does `model.tokenizer = tokenizer` outside the try/except block. The built-in tokenizer uses `AutoTokenizer.from_pretrained()` which fails because the `dual_ar` model type isn't recognized by the installed `transformers` version. The `tokenizer` variable is never assigned.

2. **Fixed the tiktoken tokenizer patch** (`voice/fish_tokenizer.py`):
   - Changed semantic token count from 1024 → 4096 (matching the model's actual codebook size)
   - This fixed the model loading — semantic IDs correctly injected: `151658-155753`
   - Model loads cleanly: `All keys matched successfully`

3. **Tested TTS after fix** — still broken, but in a consistent pattern:
   - Short text ("Hello world.") → 4 tokens (3 semantic + im_end), 0.14s click
   - Longer text ("Hello, this is a test of the fish speech system.") → 1024 tokens (max), 47s noise
   - Same behavior with and without reference audio
   - Same behavior with `COMPILE=1` (just faster: 111 GB/s vs 14 GB/s)
   - Same behavior with varied sampling params (temp 0.1–0.8, top_p 0.5–0.8, repetition_penalty 1.1–1.5)

4. **Tried `s2-pro` model** — Daniel confirmed it worked great previously but inference was too slow to be usable. Reverted back to s1-mini.

5. **Pulled latest Docker image** — already up to date (`sha256:9b451dec...`, built 2026-03-12).

### Root cause: model doesn't enter speech generation mode

**Logit analysis after the prompt** (the smoking gun):
```
Top tokens by raw logit value (no bias):
  17.875  id=339     'th'         ← TEXT token (highest!)
  17.500  id=1086    'ific'       ← TEXT token
  16.875  id=12240   'ematic'     ← TEXT token
  16.750  id=60387   ' finalized' ← TEXT token
  16.625  id=151658  semantic:0   ← first semantic token (tied with im_end)
  16.625  id=151647  IM_END       ← stop token
```

The model **strongly prefers text tokens** (logit 17–18) over semantic tokens (max 16.6). After `<|voice|>`, it wants to continue generating text ("thematic"), not speech codes. When the logit bias masks text tokens and forces semantic-only generation, it's left choosing between `semantic:0` and `IM_END` which are **tied** — producing either immediate stop or degenerate code-0 loops.

**Without logit bias**, the model's first output is `|` (pipe character, id=760) — literal text continuation, not speech at all.

### Why the model doesn't work

The `openaudio-s1-mini` checkpoint (HuggingFace, last updated 2026-02-06) appears incompatible with the Docker image's inference code (updated 2026-03-12). The model:
- Loads fine (weights match, no missing/unexpected keys)
- Encodes prompts correctly (tokenizer verified)
- Has correct semantic ID ranges (verified against `special_tokens.json`)
- But **never naturally generates semantic tokens** — it treats `<|voice|>` as text context, not a mode switch

Known GitHub issues confirm s1-mini has quality problems in self-hosted deployments:
- [#1005](https://github.com/fishaudio/fish-speech/issues/1005): Local s1-mini sounds robotic/distorted vs cloud API
- [#1136](https://github.com/fishaudio/fish-speech/issues/1136): Gibberish output, closed as stale with no fix

The container's code default is `s2-pro` (a `fish_qwen3_omni` model), not `s1-mini`. The `s1-mini` path through `DualARTransformer` may be undertested or broken in this Docker image version.

---

## Current state

### docker-compose.yml (fish-speech service)
```yaml
fish-speech:
  image: fishaudio/fish-speech:server-cuda
  volumes:
    - fish-speech-checkpoints:/app/checkpoints
    - ./voice/fish_tokenizer.py:/app/fish_speech/tokenizer.py:ro   # REQUIRED — built-in tokenizer crashes
  environment:
    - LLAMA_CHECKPOINT_PATH=checkpoints/openaudio-s1-mini
    - DECODER_CHECKPOINT_PATH=checkpoints/openaudio-s1-mini/codec.pth
    - DECODER_CONFIG_NAME=modded_dac_vq
    - COMPILE=1
```

### Voice reference files (updated this session)
- `voice_ref/hive_mind_voice.wav` — new ~10s clip (copied from `voice/voice.wav`)
- `voice_ref/hive_mind_voice.txt` — new transcript (179 chars): "I've loved it since I was a child ffff for the reasons that, what, I don't think my parents read me poetry, but I had a kind of a feeling I liked the sound of the pattern it made."
- `voice_ref/hive_mind_voice.lab` — matching .lab file (updated)

### Tokenizer patch (`voice/fish_tokenizer.py`)
- Uses tiktoken (not HuggingFace AutoTokenizer) — required because the checkpoint has `tokenizer.tiktoken`, not `tokenizer.json`
- Fixed: 4096 semantic tokens (was 1024)
- Token names match checkpoint's `special_tokens.json` (e.g., `<|end_of_text|>`, `<|im_end|>`, `<|voice|>`)

### Checkpoints on volume (`fish-speech-checkpoints`)
- `openaudio-s1-mini/` — 0.5B dual_ar model (SHA: f4b445029346701e, HF last modified 2026-02-06)
- `s2-pro/` — 5B fish_qwen3_omni model (works but too slow for real-time use)
- Both share identical `codec.pth` (md5: d3a90dbe1d535e7d)

---

## Next steps to try

1. **Check the official CLI inference path** — the docs show a 3-step CLI pipeline (`codec inference → text2semantic inference → decode`). This bypasses the API server entirely. If CLI works but API doesn't, the bug is in the server's prompt construction:
   ```bash
   # Step 1: encode reference audio
   python fish_speech/models/dac/inference.py -i ref.wav --checkpoint-path checkpoints/openaudio-s1-mini/codec.pth
   # Step 2: generate semantic tokens
   python fish_speech/models/text2semantic/inference.py --text "Hello world" --compile
   # Step 3: decode to audio (handled by codec)
   ```

2. **Try the `inference.ipynb` notebook** from the fish-speech repo — may have a working prompt format: https://github.com/fishaudio/fish-speech/blob/main/inference.ipynb

3. **Try the `v2.0.0-beta` Docker image tag** (from 2026-03-10, 2 days before current `server-cuda`) — the latest image may have introduced a regression.

4. **Check if `norm_fastlayer_input` should be `True`** for s1-mini — the s2-pro conversion sets it to `True` but s1-mini's config.json doesn't include it (defaults to `False`). This affects whether the fast AR layers receive normalized or raw hidden states.

5. **Try `--half` flag** (float16 instead of bfloat16) — unlikely to help but cheap to test.

6. **Consider alternative TTS backends** if s1-mini can't be fixed:
   - XTTS v2 (mentioned in GitHub issues as alternative)
   - Index TTS 2
   - Keep Bark as fallback (already configured)

---

## Architecture reminder

- `fish-speech` container: `fishaudio/fish-speech:server-cuda`, internal port 8080
- `voice-server` container: routes TTS to fish-speech via `TTS_BACKEND=fish` env
- `voice_server.py` sends reference audio as base64 inline in every `/v1/tts` request
- Switch TTS backend live (no restart): `POST http://voice-server:8422/backend`
- GPU: NVIDIA RTX A6000, 49GB VRAM (plenty for s1-mini at ~8GB, tight for s2-pro at ~14GB)
