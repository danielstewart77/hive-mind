---
name: commit
description: Stage, commit, push, and open a PR. Use when the user asks to commit changes, push a branch, or create a pull request. Handles master branch protection automatically.
user-invocable: true
argument-hint: "[commit message]"
---

# Commit

## IMPORTANT: master is branch-protected — never push directly to master. Always use a feature branch.

## Step 1 — Parse Arguments

- `$@` = commit message (required). If not provided, ask the user before proceeding.

## Step 2 — Assess current branch

Run `git branch --show-current`.

- Already on a feature branch (not `master`) → skip to Step 4
- On `master` → Step 3

## Step 3 — Create feature branch

Derive a branch name from the commit message:
- Lowercase, hyphens instead of spaces, strip special characters
- Prefix: `feat/` (new), `fix/` (bug), `chore/` (everything else)

```bash
git checkout -b <branch-name>
```

## Step 4 — Stage and commit

```bash
git add <relevant files>   # specific files, not git add -A
git status                 # verify staged
git commit -m "<message>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

If nothing to commit, say so and stop.

## Step 5 — Push branch

```bash
git push -u origin <branch-name>
```

## Step 6 — Create PR

```bash
gh pr create --title "<commit message>" --body "$(cat <<'EOF'
## Summary
<bullet points from commit message>

## Test plan
- [ ] Verify changes work as expected

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Return the PR URL to the user.
