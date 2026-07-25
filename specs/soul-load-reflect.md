# Soul Load + Reflect — Spec

## What this closes

A mind's identity graph grows through `--reflect` cycles but isn't read back into context at session start by default. `soul_nudge.sh` closes that loop: load graph state on turn 1, then periodically reflect against it.

## Current behavior

`soul_nudge.sh` is a `Stop` hook. It's fully synchronous — no background process, no separate `claude` subprocess invocation. It works by writing to stderr and exiting `2`, which is how a Claude Code hook feeds a command back into the *same* session's next turn:

```
turn 1                  → stderr: "/self-reflect --load", exit 2
turn N (every 5th turn) → stderr: "/self-reflect --load" then "/self-reflect --reflect", exit 2
all other turns         → no output, exit 0
group chat sessions     → always exit 0 (suppressed — output would bleed into the SSE stream)
```

Turn counting is a flat file (`$COUNTER_FILE`, default `/tmp/claude_soul_turn_counter`), incremented once per Stop event. `NUDGE_EVERY` is hardcoded to 5.

There is no background process, no `nohup`, no `--dangerously-skip-permissions`, no `--notify` flag, and no separate log file — everything happens inline in the triggering session via the hook's stderr/exit-code contract.

## Which minds have this hook

Only `minds/bob/.claude/hooks/soul_nudge.sh` exists today. `minds/ada/.claude/hooks/` has no `soul_nudge.sh` — Ada doesn't currently get this bootstrap-on-turn-1 behavior. Since mind folders are gitignored per-deployment config, whether a given mind gets this hook (and where) is a deployment decision, not something this repo enforces.

## Code References

| File | Path |
|------|------|
| Stop hook | `minds/bob/.claude/hooks/soul_nudge.sh` |
| Reflect skill | `minds/bob/.claude/skills/self-reflect/SKILL.md` |
| Turn counter | `/tmp/claude_soul_turn_counter` (or `$COUNTER_FILE`) |

## Troubleshooting

**Not seeing a load/reflect cycle:**
1. Confirm the mind actually has `.claude/hooks/soul_nudge.sh` wired into its `settings.json` `Stop` array — it's per-mind, not automatic.
2. Check `$COUNTER_FILE` is writable and not stuck at a stale count from a prior session.
3. Group chat sessions always suppress output by design — check `HIVEMIND_GROUP_SESSION` isn't set if you expected output outside a group session.
