---
name: git
description: Git workflow for Hive Mind repos. Use whenever pushing, pulling, merging, or creating branches. Master is branch-protected — always branch + PR, never push direct.
user-invocable: true
---

# Git

## hive_mind repo rules

**Master is branch-protected.** You cannot push directly to master. Always:

1. Create a feature branch
2. Commit changes
3. Push the branch
4. Create a PR via `gh pr create`
5. Merge via `gh pr merge --merge --delete-branch`
6. Checkout master and pull

```bash
git checkout -b <branch-name>
# ... make changes, commit ...
PAT=$(/opt/venv/bin/python3 -m keyring get hive-mind GITHUB_PAT 2>/dev/null)
git remote set-url origin "https://danielstewart77:${PAT}@github.com/danielstewart77/hive_mind.git"
git push -u origin <branch-name>
GH_TOKEN="$PAT" gh pr create --title "..." --body "..."
GH_TOKEN="$PAT" gh pr merge --merge --delete-branch
git checkout master && git pull origin master
```

## Auth

The GitHub PAT is stored in the keyring under `GITHUB_PAT`. Always retrieve it fresh — do not hardcode. The remote URL must include the token for push/PR operations.

## Branch naming

| Type | Pattern |
|---|---|
| Bug fix | `fix/<short-description>` |
| Feature | `feat/<short-description>` |
| Chore/cleanup | `chore/<short-description>` |
| Story work | Use the branch created by `/story-start` |

## spark_to_bloom repo

No branch protection. Direct push to main is fine:

```bash
cd /home/hivemind/dev/spark_to_bloom
git add -A && git commit -m "..." && git push
```

Files in `src/templates/` and `src/static/` are served live via bind mount — file writes are immediately live on the web without a commit or restart.
