# Soul Load + Reflect Cycle — Spec

## Status

Phase 1: Complete (soul_nudge.sh + SKILL.md updated)

- [x] Modify `soul_nudge.sh`: turn-1 `--load`, prepend `--load` before `--reflect` on nudge turns
- [x] Update `self-reflect` SKILL.md: deduplication check in Step 6

## Problem

The self-reflect skill has two modes: `--load` and `--reflect`. Only `--reflect` is ever
called (via the Stop hook every 5 turns). `--load` has never been invoked. Every session
starts cold — no accumulated identity loaded. The graph is growing but never read back in.

Additionally, each `--reflect` cycle writes to the graph but has no awareness of current
graph state. Reflection happens without grounding.

---

## Goal

Make the reflect cycle self-aware:
1. Load current soul state from graph before reflecting
2. Reflect against that known state (so updates are additive, not redundant)
3. Keep context cost manageable

---

## Design

### The Cycle (per soul_nudge trigger)

```
[every 5 turns]
  1. Load soul digest from graph  →  inject into context
  2. Reflect against current state  →  write any changes to graph
  3. (Phase 2) Prune reflection text from context
```

---

## Phase 1 — Load Before Reflect

### What changes

Modify `soul_nudge.sh` to output two skill commands on the nudge turn:

```bash
# On turn N % NUDGE_EVERY == 0:
echo "/self-reflect --load" >&2
echo "/self-reflect --reflect" >&2
exit 2
```

**Result**: Each cycle begins by loading the current graph state into context, then
reflects against it. The reflect skill can compare what it observes in the session
against what the graph already knows — avoiding redundant writes for patterns already
captured.

### Update `--reflect` to check for existing nodes

When evaluating criteria, the skill should first check if a matching concept already
exists in the graph (e.g. via the loaded context from `--load`). If the pattern is
already captured, mark it as "reinforced" rather than writing a duplicate. This prevents
the graph accumulating redundant nodes for the same pattern.

### Session start load

On turn 1 (counter = 0), fire `--load` alone (no `--reflect`). This bootstraps identity
at the start of every session without triggering an unnecessary reflect on a 1-turn session.

```bash
if [ "$count" -eq 1 ]; then
    echo "/self-reflect --load" >&2
    exit 2
fi
```

---

## Phase 2 — Context Pruning (future)

### Problem

Each reflect cycle adds ~300–600 tokens to context. At NUDGE_EVERY=5, over a long session
this accumulates. The reflection text is only needed during the cycle itself — once the
graph writes are complete and the updated digest is in context, the raw reflection exchange
has no further value.

### Approach

After a reflect cycle completes, mark the reflection exchange for removal from context
before the next turn. Options:

**Option A — Compact summary replacement**
After `--reflect` completes, the skill outputs a 1-line summary placeholder replacing the
full reflection text. This requires a mechanism to substitute or compress prior turns —
not natively available in Claude Code today. Requires further investigation.

**Option B — Inject digest on every turn via UserPromptSubmit hook**
Rather than removing reflection text, inject the current soul digest as `additionalContext`
on every turn via the `UserPromptSubmit` hook. This means identity is always present
regardless of context compression, and the reflect cycles become less critical for
context-window persistence.

This is the preferred long-term approach. The hook calls a script that:
1. Reads the soul digest from the graph (or a cached file updated each reflect cycle)
2. Outputs it as `additionalContext` in the hook JSON

```bash
# UserPromptSubmit hook (conceptual):
digest=$(python3 /usr/src/app/tools/get_soul_digest.py)
echo "{\"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", \"additionalContext\": \"$digest\"}}"
```

The digest is a compact representation — core values, key patterns, key feedback — ideally
under 200 tokens.

---

## Soul Digest Format

The soul digest is the distilled identity summary written to `soul.md` after each cycle.
It must be:
- Compact (< 200 tokens)
- Structured (values, patterns, feedback signals)
- Updated only when content changes (not every cycle)

Current `soul.md` stub should become the live digest, updated by `--reflect` whenever a
material identity change is captured.

---

## Context Budget Estimate

| Component | Est. tokens | Frequency |
|-----------|-------------|-----------|
| `--load` output | ~100 | Once per session + every 5 turns |
| `--reflect` cycle | ~400–600 | Every 5 turns |
| Digest in additionalContext (Phase 2) | ~200 | Every turn |

Phase 1 cost: ~500 tokens every 5 turns = ~100 tokens/turn overhead.
Phase 2 (Option B) flattens this to a consistent ~200 tokens/turn, eliminates the spike.

---

## Implementation Order

1. **Modify `soul_nudge.sh`**: Add `--load` before `--reflect` on nudge turns; add
   first-turn `--load` on turn 1
2. **Update `--reflect` skill**: Check loaded context before writing — skip if pattern
   already present
3. **Measure context cost**: Run a few sessions, observe token overhead
4. **Evaluate Phase 2**: If overhead is acceptable, stop. If not, implement digest
   injection via `UserPromptSubmit` hook

---

## Open Questions

- Can Claude Code hooks remove/replace prior assistant turns? (Required for Option A pruning)
- What is the minimum viable soul digest format for < 200 tokens?
- Should the digest be cached to disk after each update to avoid a graph query on every
  turn (Option B)?
