---
name: remember
description: >
  Save a piece of information to contextual memory. Trigger when the user
  says "remember [something]", "remember this", or "/remember [something]".
  Classifies the content via the local Ollama classifier and writes to lucent.
---

# remember

## Step 0 — Announce

Print as the first line of your response:

```
using skill: remember
```

## Step 1 — Run the backend script

Pipe the user's content into `"${CODEX_HOME:-$HOME/.codex}/skills/remember/remember.sh"` via stdin.
The script handles the entire pipeline (classify via hive-tools
`/ollama/structured`, save via lucent `/memory/store`) and prints a summary.

Use the Bash tool. Avoid shell-quoting issues by writing the content to a
temp file first, then redirecting it into the script's stdin:

```bash
cat <<'__REMEMBER_INPUT__' | bash "${CODEX_HOME:-$HOME/.codex}/skills/remember/remember.sh"
<the user's content, verbatim>
__REMEMBER_INPUT__
```

(Replace the `<the user's content, verbatim>` placeholder with the actual
content. The heredoc terminator `__REMEMBER_INPUT__` is unlikely to appear
in user content; if it might, pick a different sentinel.)

## Step 2 — Report to user

The script prints one of:

- `PASS: data_class=<class> action=save-vector entry_id=<id>` followed by `reason: <classifier reason>`
- `DISCARD: classifier returned action=<a> class=<c> reason=<r>` (the classifier judged it not worth saving)
- `FAIL: <reason>` (Ollama or lucent error; run dir preserved for inspection)

Pass the script's output back to the user verbatim, or summarise in one
sentence — the user just needs to know what landed.
