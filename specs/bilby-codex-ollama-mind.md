# Bilby Codex Ollama Implementation

## Constraints

- Preserve:
  - `name: bilby`
  - `mind_id: 37cd48f9-1ed5-4875-91c1-a3b0464deafc`
  - `gateway_url: http://bilby:8420`
  - Bilby's prompt files
  - Bilby's soul loading and prompt composition behavior
  - Bilby's HTTP route contract:
    - `GET /health`
    - `GET /sessions`
    - `POST /sessions`
    - `POST /sessions/{sid}/message`
    - `POST /sessions/{sid}/interrupt`
    - `DELETE /sessions/{sid}`
- Remove dependency on `claude_code_sdk`
- Use Nagatha's Codex CLI session pattern
- Use Bob's runtime env injection pattern for Ollama routing

## Required Runtime Shape

Update `minds/bilby/runtime.yaml` to this shape:

```yaml
harness: codex_cli
provider: ollama
resume_policy: provider-local
transport:
  type: codex_exec_json
env:
  OLLAMA_BASE_URL: http://<ollama-host>:11434/v1
```

Keep existing Bilby identity fields and prompt file list unchanged unless implementation requires an additional Codex-specific prompt file.

## Phase 1

### Implement

- Edit `minds/bilby/runtime.yaml`
- Replace:
  - `harness: claude_sdk` -> `harness: codex_cli`
  - `provider: anthropic` -> `provider: ollama`
  - `resume_policy: always` -> `resume_policy: provider-local`
  - `transport.type: sdk` -> `transport.type: codex_exec_json`
- Add `env:` entries required to define the remote Ollama Responses endpoint for Codex
- Keep Bilby's `name`, `mind_id`, `gateway_url`, `description`, and `prompt_files`

### Test

- Read back `minds/bilby/runtime.yaml`
- Verify the old SDK settings are gone
- Verify the Ollama env block is present
- If verification fails, fix before continuing

## Phase 2

### Implement

- Rewrite `minds/bilby/implementation.py`
- Use `minds/nagatha/implementation.py` as the transport reference
- Keep Bilby's existing:
  - startup secret fetch
  - soul fetch
  - prompt assembly
  - FastAPI route set
- Remove:
  - all `claude_code_sdk` imports
  - `_run_sdk_turn`
  - SDK-specific session state
- Replace Bilby session state with:
  - `system_prompt`
  - `thread_id`
  - `model`
- First turn behavior:
  - spawn `codex exec --json --dangerously-bypass-approvals-and-sandbox --model <model> -`
  - send `system_prompt + separator + user content` to stdin
- Resume behavior:
  - spawn `codex exec --json --dangerously-bypass-approvals-and-sandbox --model <model> resume <thread_id> -`
  - send user content to stdin
- Build subprocess env with:

```python
env = os.environ.copy()
env.update({k: str(v) for k, v in RUNTIME_ENV.items()})
```

- Add Codex provider overrides on the subprocess command line:
  - `-c 'model_provider="bilby_ollama"'`
  - `-c 'model_providers.bilby_ollama.name="Bilby Ollama"'`
  - `-c 'model_providers.bilby_ollama.base_url="<OLLAMA_BASE_URL>"'`
- Run subprocess with `cwd=/usr/src/app`
- Parse Codex JSON output using Nagatha's event pattern:
  - `thread.started` -> store `thread_id`
  - `item.completed` with `agent_message` -> emit assistant SSE
  - `turn.completed` -> emit result SSE
  - `turn.failed` -> emit error result SSE
- Keep `/interrupt` non-destructive and transport-informational, matching Nagatha's current semantics

### Test

- Run a syntax check on `minds/bilby/implementation.py`
- Search the file for `claude_code_sdk`
- Verify no SDK references remain
- Verify `codex exec` appears in the new implementation
- If verification fails, fix before continuing

## Phase 3

### Implement

- Verify Bilby's container/runtime assumptions are compatible with Codex CLI
- If Bilby needs a Codex home directory, add the minimum required change only
- Do not change Bilby's service name, gateway URL, or container command unless required for Codex execution
- Keep filesystem mounts unchanged unless Codex cannot run without a dedicated config mount

### Test

- Inspect:
  - `minds/bilby/container/compose.yaml`
  - any Bilby-specific config path referenced by the new implementation
- Verify the implementation does not still depend on Claude SDK-only assets
- Verify any new Codex-specific path referenced in code exists or is created safely
- If verification fails, fix before continuing

## Phase 4

### Implement

- Update documentation that states Bilby is SDK-backed
- Minimum files to check:
  - `README.md`
  - `CLAUDE.md`
  - `docs/multi-mind.md`
  - `docs/multi-mind-architecture.md`
- Replace descriptions so Bilby is documented as:
  - Codex CLI transport
  - Ollama-backed
  - Bilby identity unchanged

### Test

- Search repo for:
  - `claude_code_sdk`
  - `Claude SDK`
  - `sdk-backed`
  - Bilby-specific SDK wording
- Verify remaining hits are either historical notes or intentionally unchanged archives
- If a live docs file still misstates Bilby's implementation, fix before continuing

## Phase 5

### Implement

- Run an end-to-end Bilby validation through its HTTP interface
- Validate:
  - session creation
  - first-turn response
  - resume-turn response
  - interrupt endpoint response
  - session deletion

### Test

- `POST /sessions`
- `POST /sessions/{sid}/message` for first turn
- `POST /sessions/{sid}/message` for resumed turn
- `POST /sessions/{sid}/interrupt`
- `DELETE /sessions/{sid}`
- Confirm:
  - SSE assistant frames are emitted
  - result frame is emitted
  - `thread_id` persists across turns
  - Ollama-routed Codex subprocess starts successfully
- If any validation fails, return to the failing phase, fix, and rerun from that phase

## Completion Criteria

- Bilby no longer uses Claude SDK
- Bilby uses Codex CLI per-turn subprocess execution
- Bilby routes model calls to Ollama through runtime env injection
- Bilby's external identity and HTTP surface remain intact
- Phase 5 end-to-end validation passes
