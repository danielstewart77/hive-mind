---
name: rotate-session
description: Force the mind's rotation pipeline to run right now, bypassing the token-threshold gate. Use when the conversation has drifted out of sync with the user and a fresh session is the cheapest reset.
user-invocable: true
---

# /rotate-session

Trigger the same pipeline the Stop hook runs (`~/.claude/hooks/rotation_check.py`), with the token threshold overridden to 0 so it fires regardless of transcript size.

1. Run the hook with threshold disabled and a stdin event built from the current session env:

```bash
python3 ~/.claude/hooks/rotation_check.py <<JSON
{"session_id":"$CLAUDE_CODE_SESSION_ID","transcript_path":"$HOME/.claude/projects/<repo-root-slug>/$CLAUDE_CODE_SESSION_ID.jsonl","force":true}
JSON
```

(`<repo-root-slug>` is the repo root path with `/` replaced by `-`, as
Claude Code names its per-project transcript directories.)

2. Tell the user: rotation is in flight detached; this session will be killed and respawned in ~30-90 s. The new session picks up with the carry-forward summary.
