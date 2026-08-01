---
name: always-remember
description: >
  Save a piece of information to the standing tier — always-on, loaded at
  every session bootstrap. Reserved for behavioural rules and high-signal
  invariants. Trigger when the user says "always remember [something]" or
  "/always-remember [something]". Skips classification (override) and writes
  directly to lucent with tier=standing.
---

# always-remember

## Step 0 — Announce

Print as the first line of your response:

```
using skill: always-remember
```

## Step 1 — Determine scope: shared or self

Every standing rule is written either to **this mind only** or to
**every mind** (the `shared` mind_id sentinel). Decide which based on
the user's turn.

Use these signals — they're usually unambiguous:

- **Shared** (every mind):
  - "always remember to keep responses concise" — clearly a
    behavioural style rule that applies to any mind.
  - "every mind should ..." / "all of you should ..." /
    "the whole hive ..." — explicit cross-mind language.
  - Universal house-style: markdown formatting, response length,
    tone of voice, security reflexes.
- **Self** (this mind only):
  - "this mind, remember you have full host access" — mind-specific
    capability or constraint.
  - "you specifically should ..." — explicit self-reference.
  - Anything tied to this mind's environment (hardware, files, role).

**When in doubt, ask.** Say something like:

> Save this for every mind, or just for me?

Then proceed once the user answers.

## Step 2 — Run the backend script

Pipe the rule into `"${CODEX_HOME:-$HOME/.codex}/skills/always-remember/always_remember.sh"` via stdin.
Pass the scope as the first arg: `shared` or `self`.

The script POSTs directly to lucent with `tier=standing`,
`source=always-remember`, `data_class=feedback`, and either
`mind_id=shared` or `mind_id=<this mind's UUID>` based on the scope.

Use the Bash tool, heredoc form to avoid shell-quoting issues:

```bash
cat <<'__ALWAYS_REMEMBER_INPUT__' | bash "${CODEX_HOME:-$HOME/.codex}/skills/always-remember/always_remember.sh" shared
<the rule, verbatim>
__ALWAYS_REMEMBER_INPUT__
```

…or `self` for mind-only.

## Step 3 — Report to user

The script prints:

- `PASS: tier=standing scope=<self|shared> mind_id=<id> entry_id=<id>` followed by `standing-tier count: <n> (soft cap: 10)`
- `FAIL: <reason>` on lucent write error.

Summarise in one sentence to the user, including whether it was saved
as shared or self-only and the current standing-tier count.

## Notes

- **Tier guard (REQ-028):** lucent enforces `tier=standing` requires
  `source=always-remember`. The script hardcodes the source. This skill
  is the only legitimate writer to the standing tier.
- **Shared rules are visible to every mind.** They show up in every
  mind's standing-rules block at session spawn. Use sparingly — too
  many shared rules and every mind's prompt bloats. this mind-specific
  rules (host access, bare-metal nuance) belong in `self` scope.
- **The standing-tier soft cap (10) is per-mind-id.** Shared rules
  and per-mind rules count against their own scope's cap.
