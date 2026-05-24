# Per-Mind Hooks

Each mind installs three harness hooks that drive memory capture,
contextual retrieval, and session rotation. The same three scripts run
on every mind; only the harness's hook-config format differs.

## Hooks by lifecycle event

| Event | Script | What it does |
|---|---|---|
| `Stop` | `auto_remember.sh` | Per-turn memory capture and soul self-reflection |
| `Stop` | `rotation_check.py` | Token-threshold check; on hit, writes carry-forward and triggers `/clear` |
| `UserPromptSubmit` | `contextual_retrieval.sh` | Top-3 similarity search over the `feedback` data class; injects `<behavior-rules>` |
| `SessionStart` *(Claude-CLI minds only)* | `plugin_skills_sync.sh` | Symlinks plugin-provided skills into `~/.claude/skills/` |

`SessionStart` does no prompt composition — `hive-comms` already ships
the composed `system_prompt_blocks` in the dispatch payload, so the
mind has no work to do at session start beyond plugin-skill discovery.

## What each hook does

### `auto_remember.sh` (Stop)

Pure bash + jq + curl, runs in a detached subshell so the hook returns
instantly. The script:

1. Reads the transcript path from the hook event and extracts the last
   real user message and the last assistant text response.
2. **Capture branch** — classifies the turn pair against the data-class
   specs in `specs/data-classes/` via hive-tools `/ollama/structured`,
   then POSTs each `save-vector` verdict to lucent `/memory/store` with
   `tier=contextual`, `source=session`, `mind_id=$MIND_ID`.
3. **Soul self-reflect branch** — fetches the mind's current
   `soul_values` from the KG, asks Ollama whether the turn warrants a
   soul addition. On `update=true`, merges the new list back via
   `POST /graph/properties/merge` (additive, preserves other
   properties). Default verdict is `update=false`.

The hook reads `MIND_ID` (the UUID) for every lucent `mind_id=…` field
and `MIND_NAME` only for entity-name capitalisation and log paths.

### `rotation_check.py` (Stop)

Self-contained Python — talks to NS over HTTP, no imports from the
mind's code. Forks a detached child and exits immediately.

The child:

1. Counts tokens across the transcript jsonl.
2. If the count crosses the rotation threshold, runs three structured
   Ollama calls to produce a summary, project context, and state
   snapshot.
3. POSTs the envelope to `POST <COMMS_URL>/sessions/{claude_sid}/rotation-memory`
   so the next session for this `(mind_id, client_ref)` pair picks it
   up as the `<session-memory>` block.
4. POSTs `/clear` to `POST <COMMS_URL>/command` to trigger a session
   rotation on the mind.

Below the threshold the child exits without writing anything.

### `contextual_retrieval.sh` (UserPromptSubmit)

Pure bash + jq + curl. On every user prompt the hook:

1. Reads the user prompt from the hook event.
2. Issues `GET /memory/retrieve?query=<prompt>&data_class=feedback&k=3&min_score=0.65`
   against lucent.
3. If any rows return, wraps them as bullets inside `<behavior-rules>…</behavior-rules>`
   and emits `{"systemMessage": "<block>"}` so the harness injects it as
   a system message on this turn.

The lookup is unconditional; threshold filtering happens at the
endpoint. No `mind_id` filter is applied to reads — every mind sees
every feedback rule.

## Harness wiring

### Claude CLI (Ada, Bob)

`.claude/settings.json` declares the hooks in native JSON:

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "bash /usr/src/app/minds/<name>/.claude/hooks/plugin_skills_sync.sh", "timeout": 10}]}
    ],
    "Stop": [
      {"hooks": [
        {"type": "command", "command": "bash /usr/src/app/minds/<name>/.claude/hooks/auto_remember.sh"},
        {"type": "command", "command": "python3 /usr/src/app/minds/<name>/.claude/hooks/rotation_check.py"}
      ]}
    ],
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "bash /usr/src/app/minds/<name>/.claude/hooks/contextual_retrieval.sh"}]}
    ]
  }
}
```

Each mind keeps its own copies of all four hooks under
`minds/<name>/.claude/hooks/`. The scripts are byte-identical across
minds today, but the files are physically separate so changes to one
mind's hooks never affect another.

### Codex CLI (Bilby, Nagatha)

`.codex/config.toml` enables hooks and declares them per event:

```toml
[features]
codex_hooks = true

[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "bash /usr/src/app/minds/<name>/.codex/hooks/contextual_retrieval.sh"
timeout = 5

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "bash /usr/src/app/minds/<name>/.codex/hooks/auto_remember.sh"

[[hooks.Stop.hooks]]
type = "command"
command = "python3 /usr/src/app/minds/<name>/.codex/hooks/rotation_check.py"
```

Codex addresses scripts by absolute path; each Codex-CLI mind keeps its
own copies of the three hook scripts inside its `.codex/hooks/`
directory. Claude CLI and Codex emit the same hook event JSON shape,
so a single script body serves both harnesses — but every mind owns
its own copy.

## Logs

Every hook writes its own JSONL log under
`minds/<name>/data/auto-remember/`:

- `runs.jsonl` — one line per capture/classify run, with the chosen
  data classes, scores, and lucent write IDs.
- `soul_updates.jsonl` — one line per Branch B run, including `update`
  verdict and reason.
- `rotation.log` — token counts, threshold crossings, dispatched
  envelope IDs.

These volumes are mounted into the mind container via
`AUTO_REMEMBER_LOG_DIR=/usr/src/app/minds/${MIND_NAME}/data/auto-remember`
so the host can tail them.
