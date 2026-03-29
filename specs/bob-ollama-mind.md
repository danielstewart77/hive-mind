# Bob — Ollama-backed Mind

## Overview

Add Bob as a fourth mind in Hive Mind, backed by a local Ollama instance via the Claude Code CLI harness. Bob uses the same CLI subprocess architecture as Ada (Skippy's config slot), with Ollama-specific env vars injected per-subprocess to redirect Claude Code's API calls to the local Ollama server.

## User Requirements

- Bob participates in group chat alongside Ada, Nagatha, and future minds
- Bob runs on a local Ollama model (`gpt-oss:20b-32k` by default)
- The model is switchable per-session or per-config without code changes
- Bob has his own soul file and identity

## User Acceptance Criteria

- [ ] Bob responds in group chat when addressed
- [ ] Bob uses `gpt-oss:20b-32k` as the default model
- [ ] Model can be changed in `config.yaml` without code changes
- [ ] Bob's soul file is loaded correctly at session start
- [ ] `ollama launch claude` env var approach works end-to-end in the container
- [ ] Bob appears in `available_minds` and responds correctly in `/moderate` sessions

## Technical Specification

### Approach

Use the Claude Code CLI harness with Ollama env vars injected per-subprocess. This is the same pattern as Ada (`minds/ada/implementation.py`) with three env var overrides:

```
ANTHROPIC_AUTH_TOKEN=ollama
ANTHROPIC_API_KEY=""
ANTHROPIC_BASE_URL=http://203.0.113.10:11434
```

These are already defined in `config.yaml` under `providers.ollama.env`. The `ollama launch claude` command sets these automatically, but since our gateway injects env vars per-subprocess, we set them directly and invoke `claude` normally.

### Model Flag

Pass `--model gpt-oss:20b-32k` (or whatever is configured in `config.yaml`) to the Claude CLI subprocess.

### Context Window

The `gpt-oss:20b-32k` model has a 32k context window, which meets Ollama's minimum recommendation (64k preferred — monitor if context issues arise).

### config.yaml

```yaml
bob:
  backend: cli_ollama
  model: gpt-oss:20b-32k
  soul: souls/bob.md
  db: data/bob.db
```

Add `bob` to `group_chat.available_minds`.

### Soul File

Create `souls/bob.md` — Bob's identity, tone, and role within the collective.

## Code References

| File | Change |
|------|--------|
| `config.yaml` | Add `bob` mind, add to `available_minds` |
| `minds/bob/implementation.py` | New file — CLI harness with Ollama env vars |
| `souls/bob.md` | New file — Bob's soul |
| `server.py` | Register `bob` backend type `cli_ollama` if not already handled |

## Implementation Order

1. Create `souls/bob.md` with Bob's soul content
2. Add `bob` to `config.yaml` minds and `available_minds`
3. Implement `minds/bob/implementation.py` (clone of Ada's CLI impl, with Ollama env var injection)
4. Register `cli_ollama` backend in gateway if needed
5. Restart server, test Bob in group chat
6. Verify model is switchable via config change + restart
