---
name: update-documentation
description: Update README and linked docs to match the current state of the codebase. Use when a code change has been made and docs may be stale, or when a specific doc error is spotted.
user-invocable: true
argument-hint: "[optional: feature name, commit SHA, or description of doc issue]"
---

# Update Documentation

## Step 0 — Determine scope

Before touching anything, assess what kind of update this is.

**If a context argument was provided:**
- Classify it: is this about a code change, or a specific doc error?
- Code change → Step 1A
- Doc error → Step 1B

**If no argument was provided:**
- Run `git log --oneline -10`
- Identify commits that likely affect docs (new features, renames, removals, architectural changes)
- Treat those commits as context → Step 1A

**Scope decision — make this before doing any edits:**
- Specific thing (one feature, one file, one paragraph) → **surgical mode**: fix only what's affected, then stop
- Broad (full audit, major refactor) → **full sweep mode**: walk every linked doc

State which mode you're in before proceeding.

---

## Step 1A — Code-change entry point

1. If a commit SHA or feature name was given, run `git show <sha> --stat` or `git log --oneline --all | grep <feature>`
2. Identify which files/modules changed
3. Search docs for references to those files/modules:
   ```bash
   grep -r "<module or feature name>" docs/ specs/ README.md CLAUDE.md
   ```
4. For each doc that references the changed area → Step 2

---

## Step 1B — Doc-issue entry point

1. Identify which file and section the issue is in
2. Read that file in full
3. Locate the stale/incorrect content
4. Verify the correct state by reading the actual source (code file, config, etc.)
5. Fix the specific issue → Step 3 (check for ripple effects)

---

## Step 2 — Update affected docs

For each doc identified in Step 1A:

- Read it in full
- Find the section(s) that reference the changed code/feature
- Verify the correct state by reading the actual source
- Edit only what's wrong — do not rewrite for style or reorganise
- Note each change made

Key files to check (in priority order):
- `README.md` — architecture diagram, file structure, API table, Adding New Tools section
- `CLAUDE.md` — file structure listing, design principles
- `specs/hive-mind-architecture.md` — patterns and architectural decisions
- `specs/tool-migration.md` — stateless/stateful hybrid pattern
- Any other spec that grep found a reference in

---

## Step 3 — Check for ripple effects

After any edit, check whether the changed doc links to other docs that may also need updating:

```bash
grep -o '\[.*\](.*\.md)' <edited-file>
```

For each linked file, ask: could the change you just made require an update there too? If yes, read it and fix. If no, move on.

In surgical mode: stop after one level of ripple checking.
In full sweep mode: follow links recursively until no more updates are needed.

---

## Step 4 — Verify no broken links

```bash
grep -rh '\[.*\](.*\.md)' docs/ specs/ README.md CLAUDE.md | grep -o '([^)]*\.md)' | tr -d '()' | sort -u
```

For each unique `.md` path found, verify the file exists. Report any that don't.

---

## Step 5 — Summarise

Output:
- Mode used (surgical / full sweep) and why
- Files changed, with a one-line description of what was fixed in each
- Any broken links found and whether they were fixed
- Anything that couldn't be verified automatically and needs human review
