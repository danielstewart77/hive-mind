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

`soul_nudge.sh` is a Stop hook that fires after each turn. It increments a counter and emits skill commands via stderr with `exit 2`.

**Current behaviour:** Only emits `/self-reflect --reflect` on nudge turns. Never calls `--load`.

**Phase 1 behaviour:**

```
turn 1  → emit /self-reflect --load only (bootstrap, no reflect)
turn N (nudge) → emit /self-reflect --load, then /self-reflect --reflect
all other turns → no output (unchanged)
```

The `--reflect` skill already loads context when `--load` has been run in the same session cycle. Phase 1 adds an explicit check: before calling `graph_upsert_direct`, query loaded context for an existing node with the same concept. If found, log "reinforced" and skip the write.

### Data flow

```
[Stop hook] → soul_nudge.sh
  turn == 1  → /self-reflect --load
  turn % N == 0 → /self-reflect --load → /self-reflect --reflect
                         ↓
                   graph_query(Ada)
                   inject identity context
                         ↓
                   evaluate 5 criteria
                   for each "yes": check loaded context first
                   if node exists → reinforce (no write)
                   if node new → graph_upsert_direct
```

## Code References

| File | Change |
|------|--------|
| `/home/hivemind/.claude/hooks/soul_nudge.sh` | Add turn-1 `--load`; add `--load` before `--reflect` on nudge turns |
| `/usr/src/app/.claude/skills/self-reflect/SKILL.md` | Step 6: check loaded context before writing; mark as "reinforced" if already present |

## Implementation Order

1. Modify `soul_nudge.sh`: add turn-1 load; prepend `--load` to nudge-turn output
2. Update `self-reflect` SKILL.md: in Reflect Mode Step 6, add loaded-context check before each `graph_upsert_direct`
3. Mark Phase 1 complete in `plans/soul-load-reflect.md`
4. Commit, push, PR, merge
