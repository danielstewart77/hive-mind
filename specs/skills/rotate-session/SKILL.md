---
name: rotate-session
description: Force the mind's rotation pipeline to run right now, bypassing the token-threshold gate. Use when the conversation has drifted out of sync with the user and a fresh session is the cheapest reset.
harness:
  claude:
    user-invocable: true
---

# /rotate-session

Trigger the same pipeline the Stop hook runs (`rotation_check.py` in the running harness's own hooks directory), with the token threshold overridden to 0 so it fires regardless of transcript size.

1. Run the hook with threshold disabled and a stdin event built from the current session env.

Under Claude:

```bash
python3 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/rotation_check.py" <<JSON
{"session_id":"$CLAUDE_CODE_SESSION_ID","transcript_path":"$HOME/.claude/projects/<repo-root-slug>/$CLAUDE_CODE_SESSION_ID.jsonl","force":true}
JSON
```

(`<repo-root-slug>` is the repo root path with `/` replaced by `-`, as
Claude Code names its per-project transcript directories.)

Under Codex:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/rotation_check.py" <<JSON
{"session_id":"$CODEX_THREAD_ID","force":true}
JSON
```

Codex writes one rollout file per thread under `$CODEX_HOME/sessions/`, so
the hook locates the transcript from the thread id rather than being handed
a path.

2. Tell the user: rotation is in flight detached; this session will be killed and respawned in ~30-90 s. The new session picks up with the carry-forward summary.
