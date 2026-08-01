---
name: end-session
description: Harvest durable memory from the current Codex conversation, then permanently close its Hive Comms gateway session without spawning a replacement. ALWAYS trigger when the operator's message is "/end-session", "$end-session", or "end session" — those are hard rules, not hints. Also trigger when he asks to end, kill, close, or permanently wrap up the current session.
---

# End Session

Harvest durable facts, schedule the gateway session's closure, then send one
final reply. Do not rotate or spawn a replacement.

## Harvest

Review the complete conversation. Extract each durable fact separately as:

- `feedback` for preferences, corrections, and behavioral rules.
- `current-state` for facts about systems, code, configuration, people, or
  minds as they exist now.
- `future-state` for plans and intended changes that have not shipped.

Skip trivia, temporary status, and facts already present in memory context.
For each new fact, run:

```bash
printf '%s' '<one durable fact>' | \
  python3 $CODEX_HOME/skills/end-session/scripts/store-memory.py '<data-class>'
```

Require every memory write to succeed. If one fails, report the failure and do
not close the session.

## Close the Codex gateway session

The mind stamps the gateway's own session id on the harness process as
`HIVE_SESSION_ID`; failing that, Hive Comms stores Codex's `CODEX_THREAD_ID`
in the session's `harness_sid`. Use the bundled helper, which prefers the
former and falls back to an exact match on the latter. It never guesses by
recency, mind, or owner:

```bash
python3 $CODEX_HOME/skills/end-session/scripts/close-session.py
```

On the fallback path the helper fails unless exactly one live session matches
the current Codex thread. It starts the delayed delete in a new process
session so Codex's turn
cleanup cannot kill it. If it fails, report the error and do not claim the
session is closing.

Send one brief final message stating what was harvested and that the session
is closing. Run no more tools after scheduling the closure.
