---
name: update-documentation
description: Update README and all linked documentation to reflect the current state of the codebase
user-invocable: true
argument-hint: "[focus area or 'full']"
---

# Update Documentation

Keep the README and all linked docs accurate and current. Run this after significant architectural changes, new tools, or feature additions.

## Step 0 — Build a file inventory

Before touching anything, scan the documentation directories to build a ground-truth index of what actually exists. This is used in Step 5 to verify links — never assume a file exists without checking against this inventory first.

```bash
find /usr/src/app/docs /usr/src/app/specs /usr/src/app/plans /usr/src/app/minds \
     -name "*.md" 2>/dev/null | sort
```

Also include top-level files:
```bash
ls /usr/src/app/*.md 2>/dev/null
```

Hold this inventory in context. Every link verification in Step 5 must resolve against this list, using the correct base directory for each source file (i.e. relative paths resolve from the file's own directory, not from the repo root).

## Step 1 — Understand what changed

Before touching any file, gather context:
- Read `README.md` in full
- Read `CLAUDE.md` in full
- Read `specs/hive-mind-architecture.md`
- Run `git log --oneline -20` to see recent commits
- If a focus area was provided (e.g. "tools", "telegram"), scope the update to that area

## Step 2 — Audit README

Check each section of README.md against reality:
- **Architecture diagram** — does it reflect the current container structure, MCP setup, and client list?
- **File structure** — does it match actual directory layout? (`tools/stateless/`, `tools/stateful/` moved from `agents/`)
- **Quick Start** — still accurate?
- **Configuration** — matches `config.yaml`?
- **Gateway API table** — matches `server.py` endpoints?
- **Adding New Tools** section — reflects hybrid stateless/stateful pattern?

Update any stale sections directly.

## Step 3 — Follow all linked docs

Extract every `.md` link from README.md, then for each:
1. Read the linked file
2. Check for stale references (old paths, removed tools, renamed modules)
3. Update if needed

Priority files to check:
- `specs/hive-mind-architecture.md` — architecture decisions and patterns
- `specs/tool-migration.md` — stateless/stateful hybrid pattern
- `specs/conventions.md` — coding conventions
- `specs/INDEX.md` — index of all specs
- `docs/` subdirectories — human-readable background docs

## Step 4 — Check CLAUDE.md

Verify `CLAUDE.md` file structure section matches reality:
- Are all listed files still present at the stated paths?
- Are new significant files missing from the listing?
- Does the "Adding New Tools" section reflect current patterns?

## Step 5 — Verify cross-references

Using the inventory from Step 0, check every `.md` link in docs/, specs/, and README.md:

```bash
grep -rh '\[.*\](.*\.md)' /usr/src/app/docs/ /usr/src/app/specs/ /usr/src/app/README.md \
  | grep -o '([^)]*\.md)' | tr -d '()' | sort -u
```

For each link found, resolve it **relative to the file it appears in** (not from repo root), then check that path against the Step 0 inventory. Report:
- Links that resolve correctly — skip
- Links that don't resolve — flag as broken, fix if the correct target is clear from context

Do not use a flat path resolver. Always compute: `dirname(source_file) + "/" + link_path` and normalise.

## Step 6 — Summarise changes

After all updates, output:
- Which files were changed and why
- Any links that were broken and fixed
- Any sections that were stale and what was corrected
- Anything that couldn't be verified automatically (needs human review)

## Notes

- Do not rewrite docs for style — only correct facts
- Preserve existing structure and tone
- If unsure whether something is stale, read the referenced source file before changing
- Do not update `soul.md` — that has its own update criteria
