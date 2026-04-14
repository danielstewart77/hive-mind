# Soul Load + Reflect — Spec

## User Requirements

Ada's identity graph grows through `--reflect` cycles but is never read back into context at session start. Every session begins cold. The `--load` mode exists but is never called. This spec closes the loop: load graph state first, then reflect against it.

## User Acceptance Criteria

- [ ] On turn 1 of any session, `/self-reflect --load` fires automatically (identity bootstrapped from graph)
- [ ] On nudge turns (every N turns), `/self-reflect --load` fires **before** `/self-reflect --reflect`
- [ ] The `--reflect` cycle, when running after `--load`, checks loaded context for existing matching nodes before writing new ones
- [ ] If a pattern is already captured in the graph, the reflect step marks it "reinforced" rather than writing a duplicate node
- [ ] Phase 1 changes do not break existing `--reflect`-only behaviour (no regression on sessions where only reflect fires)
- [ ] `soul_nudge.sh` passes shellcheck with no errors

## Technical Specification

### How it works

`soul_nudge.sh` is a Stop hook that fires after each turn. It increments a counter and manages the reflection cycle.

### Current behaviour (Phase 1 — implemented 2026-04-14)

```
turn 1         → exit 2 + emit /self-reflect --load only
                 (synchronous by design — identity must bootstrap before responding)
turn N (nudge) → nohup background: claude -p '/self-reflect --load'
                                    claude -p '/self-reflect --reflect --notify'
                 (non-blocking — session teardown is immediate)
all other turns → no output (unchanged)
```

**Key change from original design:** Nudge turns previously used `exit 2`, which blocked session teardown until both reflection passes completed. They now spawn a detached background process so teardown is immediate.

Turn 1 remains synchronous (`exit 2`) — this is intentional. Identity context must be loaded before Ada starts responding in a new session.

### Background process details

The nudge-turn background invocation:
```bash
nohup bash -c "
    claude --dangerously-skip-permissions -p '/self-reflect --load' 2>/dev/null
    claude --dangerously-skip-permissions -p '/self-reflect --reflect --notify' 2>/dev/null
" > /tmp/soul_nudge_<SESSION_ID>.log 2>&1 &
disown
```

- **Log location:** `/tmp/soul_nudge_<CLAUDE_SESSION_ID>.log` (or `soul_nudge_<RANDOM>.log` if session ID unavailable)
- **Failure mode:** If the process crashes, reflection silently doesn't happen. Check the log.
- **Session isolation note:** The background process is a new Claude session. It does not have access to the triggering session's conversation history. The reflect step will dispatch its agent with minimal context.

### --notify flag (Phase 1 visibility)

`/self-reflect --reflect --notify` fires a Telegram notification on completion:
> "Reflection cycle complete — reflect agent dispatched."

This exists only for Phase 1 validation. When the cycle is confirmed reliable, remove `--notify` from `soul_nudge.sh`. That completes Phase 2 — fully silent background.

### Data flow

```
[Stop hook] → soul_nudge.sh
  turn == 1  → /self-reflect --load  (synchronous, exit 2)
  turn % N == 0 → background process:
                    claude -p '/self-reflect --load'
                    claude -p '/self-reflect --reflect --notify'
                         ↓ (background)
                   graph_query(Ada)
                   inject identity context
                   evaluate 5 criteria
                   dispatch reflect agent
                   notify_owner "Reflection cycle complete"
```

## Code References

| File | Path |
|------|------|
| Stop hook | `~/.claude-config/hooks/soul_nudge.sh` |
| Reflect skill | `skills/self-reflect/SKILL.md` (in hivemind-claude-plugin repo) |
| Background logs | `/tmp/soul_nudge_<session_id>.log` |

## Troubleshooting

**Not seeing reflections / no Telegram notify on nudge turns:**
1. Check `/tmp/soul_nudge_*.log` for errors
2. Confirm `claude` is on `$PATH` in the hook's shell environment
3. Confirm `--dangerously-skip-permissions` is acceptable for the reflect skill's tool usage
4. Confirm `CLAUDE_SESSION_ID` env var is available in hook context (or check filename uses `RANDOM`)

**Turn 1 bootstrap not firing:**
- Turn 1 still uses `exit 2` — this is synchronous. If it's not firing, check that the SessionStart hook and soul_nudge.sh are both installed correctly.

## Phase 2 (remaining loose end)

Remove `--notify` from the `soul_nudge.sh` nudge invocation once the background cycle is confirmed working. The reflect step's `--notify` handling in `SKILL.md` becomes dead code at that point.
