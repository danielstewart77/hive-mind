---
name: convert-claude-skill-to-codex
description: "Args: [skill-name] [output-path]. Convert a Claude Code skill to a Codex-compatible skill. Strips non-portable frontmatter fields, preserves body content, validates encoding. Use when preparing skills for the Codex harness or Codex plugin distribution."
argument-hint: "[skill-name] [output-path]"
---

# convert-claude-skill-to-codex

Convert a Claude Code SKILL.md to a Codex-compatible SKILL.md.

## Step 1 — Parse Arguments

- `$ARGUMENTS[0]` = skill name (required). The skill to convert, looked up in `.claude/skills/<name>/SKILL.md`.
- `$ARGUMENTS[1]` = output path (optional). Where to write the converted skill. Default: `.codex/skills/<name>/SKILL.md`

If skill name is missing, ask the user.

## Step 2 — Read the source skill

Read `.claude/skills/<name>/SKILL.md`. Parse the YAML frontmatter.

## Step 3 — Convert frontmatter

Strip all non-portable fields. Keep only:

| Keep | Strip |
|------|-------|
| `name` | `user-invocable` |
| `description` | `disable-model-invocation` |
| | `allowed-tools` |
| | `tools` |
| | `model` |
| | `context` |
| | `agent` |
| | `effort` |
| | `hooks` |
| | `paths` |
| | `shell` |

**Conversion rules:**

1. Keep `name` exactly as-is.
2. Keep `description` exactly as-is. If it contains `argument-hint`, preserve the `Args:` prefix pattern — `argument-hint` is a supported extension in Codex.
3. If `argument-hint` exists in the source, keep it (it's a supported extension).
4. Strip everything else from frontmatter.

**Result:** Frontmatter should have only `name`, `description`, and optionally `argument-hint`.

## Step 4 — Review body content

Scan the markdown body for Claude-specific references that may need adjustment:

- **Tool names:** Claude uses `Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob`, `Agent`, `Skill`. Codex uses the same tool names for file operations and bash. No changes needed for most tools.
- **Subagent references:** Claude uses `context: fork` and named agent types. Codex uses different subagent mechanics (`worker`, `explorer`, `default`). Flag these for manual review.
- **`/skill-name` invocations:** Claude uses `/skill-name` to invoke skills. Codex uses the same pattern. No change needed.
- **Claude-specific paths:** `.claude/skills/`, `.claude/agents/`, `~/.claude/`. Codex equivalents are `.codex/skills/` (or `.agents/skills/`), `.codex/agents/`, `~/.codex/`. Flag but do NOT auto-replace — the skill may intentionally reference Claude paths.

If any body content needs manual attention, report it at the end. Do NOT silently change body content.

## Step 5 — Write the converted skill

Create the output directory:
```bash
mkdir -p <output-path-dir>
```

Write the converted SKILL.md:
- UTF-8 without BOM
- First byte must be `-` (opening `---`)
- No blank lines before frontmatter
- Consistent line endings (LF on Linux/Mac)

## Step 6 — Validate

Verify the output file:
```bash
# Check first bytes are not BOM
head -c 3 <output-path> | xxd | grep -v "efbb bf" && echo "No BOM - OK" || echo "WARNING: BOM detected"

# Check frontmatter starts at line 1
head -1 <output-path> | grep "^---$" && echo "Frontmatter OK" || echo "ERROR: Missing frontmatter"

# Check only portable fields in frontmatter
sed -n '/^---$/,/^---$/p' <output-path> | grep -vE "^(---|name:|description:|argument-hint:|\s)" && echo "WARNING: Non-portable fields remain" || echo "Clean frontmatter - OK"
```

## Step 7 — Report

- Source: `.claude/skills/<name>/SKILL.md`
- Output: `<output-path>`
- Fields stripped: list what was removed
- Body flags: any Claude-specific references that need manual review
- Validation: pass/fail

## Batch Mode

To convert all skills at once:
```bash
for skill_dir in .claude/skills/*/; do
  name=$(basename "$skill_dir")
  /convert-claude-skill-to-codex "$name"
done
```
