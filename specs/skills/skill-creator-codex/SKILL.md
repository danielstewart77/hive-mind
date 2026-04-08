---
name: skill-creator-codex
description: Guide for creating Codex skills correctly. Use when creating new skills to ensure proper folder structure and SKILL.md formatting.
---

# How to Create a Codex Skill

## Overview

Skills are reusable instructions that extend Codex capabilities. They are stored in `$CODEX_HOME/skills` (or `~/.codex/skills` when `CODEX_HOME` is unset) and each skill is a folder containing a `SKILL.md` file.

## Required Structure

### Folder Organization

```
$CODEX_HOME/
`-- skills/
    `-- your-skill-name/        <-- Folder name = skill name
        `-- SKILL.md            <-- Must be named exactly SKILL.md (uppercase)
```

If `CODEX_HOME` is not set, use:

```
~/.codex/skills/your-skill-name/SKILL.md
```

**IMPORTANT:**
- The skill folder name becomes the skill identifier
- The skill file MUST be named `SKILL.md` (all uppercase, with `.md` extension)
- Do NOT name the file after the skill (for example, `my-skill.md`) - this will not work

### SKILL.md File Format

Every `SKILL.md` file must have two parts:

1. YAML frontmatter (required) - metadata between `---` markers
2. Markdown content (required) - the skill instructions

## YAML Frontmatter

The frontmatter should include these baseline fields:

```yaml
---
name: your-skill-name
description: If the skill has arguments, start with "Args: [..]." then describe what it does and when to use it.
user_invocable: true
---
```

### Encoding and Parser Safety (Critical)

To avoid "missing YAML frontmatter" loader errors:

- Write `SKILL.md` as UTF-8 **without BOM**.
- Ensure byte 0 is `-` from the opening `---`.
- Do not put blank lines or spaces before the opening `---`.
- Keep the frontmatter block at the very top of the file.

**Windows safe write example (UTF-8 without BOM):**

```powershell
$path = 'C:\Users\you\.codex\skills\my-skill\SKILL.md'
$text = @"
---
name: my-skill
description: Example description. Use when ...
---

# My Skill
"@
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($path, $text, $utf8NoBom)
```

**Quick preflight check (first 3 bytes should not be `EF BB BF`):**

```powershell
$bytes = [System.IO.File]::ReadAllBytes('C:\Users\you\.codex\skills\my-skill\SKILL.md')
$bytes[0..2] | ForEach-Object { '{0:X2}' -f $_ }
```

### Line Endings and File Consistency (Critical)

- Use consistent line endings in every file a skill creates or edits.
- On Windows repositories that use CRLF, normalize generated Markdown/text files to `\r\n` before finishing.
- When the target repo convention is known (from `.editorconfig`, `.gitattributes`, or nearby files), follow that convention.
- Add a post-write validation step for generated files to confirm no mixed line endings.

**Windows normalization example (convert mixed endings to CRLF):**

```powershell
$path = 'C:\repo\docs\STORY-DESCRIPTION.md'
$raw = [System.IO.File]::ReadAllText($path)
$normalized = [regex]::Replace($raw, "`r?`n", "`r`n")
[System.IO.File]::WriteAllText($path, $normalized, [System.Text.UTF8Encoding]::new($false))
```

**Byte-level validation (bare LF should be 0 for CRLF files):**

```powershell
$b = [System.IO.File]::ReadAllBytes($path)
$bareLf = 0
for ($i = 0; $i -lt $b.Length; $i++) {
    if ($b[$i] -eq 10 -and ($i -eq 0 -or $b[$i - 1] -ne 13)) {
        $bareLf++
    }
}
$bareLf
```

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | The skill identifier (should match folder name). |
| `description` | Yes | Brief description shown in skill listings. Include "Use when..." guidance so Codex knows when to apply it. If `argument-hint` exists, start description with `Args: <argument-hint>.` |
| `user_invocable` | Recommended | Set to `true` if users can invoke with `/skill-name`; set to `false` for background-only skills. |
| `argument-hint` | No* | Placeholder shown after `/skill-name` in the CLI (for example, `[story-number]`). Always include this if the skill accepts arguments. |
| `model` | No | Optional model pinning if your environment supports it. |
| `tools` | No | Optional tool list when your environment supports explicit tool declarations. |

\* `argument-hint` is required whenever the skill uses `$ARGUMENTS`.

### Arguments (`$ARGUMENTS`)

When a user invokes a skill with arguments (for example, `/my-skill 9576 some-name`), those arguments are available in the skill body via `$ARGUMENTS`.

**Syntax:**

| Variable | Resolves To |
|----------|-------------|
| `$ARGUMENTS` | The full argument string as-is |
| `$ARGUMENTS[0]` | First positional argument |
| `$ARGUMENTS[1]` | Second positional argument |
| `$ARGUMENTS[N]` | Nth positional argument (zero-indexed) |

**Rules:**
- Always add `argument-hint` to frontmatter when the skill accepts arguments.
- If `argument-hint` exists, make description start with `Args: <argument-hint>.` before the rest of the text.
- In the skill body, document which arguments are expected, required, and optional.
- Include fallback behavior when required arguments are missing (for example, ask the user).
- Use a dedicated step (for example, "Step 1: Parse Arguments") to extract and validate arguments early.

**Example frontmatter with argument-hint:**

```yaml
---
name: get-story
description: "Args: [story-number] [documents-path]. Pull an ADO story and create local documents. Use when starting work on a new story."
argument-hint: "[story-number] [documents-path]"
user_invocable: true
---
```

**Example argument parsing in the body:**

```markdown
### Step 1: Parse Arguments

Extract from `$ARGUMENTS`:

- `$ARGUMENTS[0]` = Story number (required, for example `9576`)
- `$ARGUMENTS[1]` = Documents path (optional, for example `C:\...\documents`)

If the story number is not provided, ask the user.
```

## Markdown Content

After the frontmatter, write skill instructions in Markdown:

```markdown
---
name: example-skill
description: Example skill demonstrating proper format. Use when learning to create skills.
user_invocable: true
---

# Skill Title

## Overview

Explain what this skill does and its purpose.

## Instructions

Provide clear, actionable instructions for Codex to follow.

## Examples

Include examples of how to use the skill.

## Best Practices

Add practical tips and constraints.
```

## Complete Example

Here is a complete skill that accepts arguments.

**Folder:** `~/.codex/skills/greeting-helper/SKILL.md`

```markdown
---
name: greeting-helper
description: "Args: [recipient-name] [formality]. Help format professional greetings. Use when the user needs to write a greeting."
argument-hint: "[recipient-name] [formality]"
user_invocable: true
---

# Greeting Helper

## Overview

This skill helps create professional greetings for emails and messages.

## Process

### Step 1: Parse Arguments

Extract from `$ARGUMENTS`:

- `$ARGUMENTS[0]` = Recipient name (optional - if not provided, ask the user)
- `$ARGUMENTS[1]` = Formality level: `casual`, `professional`, or `formal` (optional - defaults to `professional`)

### Step 2: Generate Greeting

Based on the formality level, generate the greeting:

- Casual: "Hi [Name],"
- Professional: "Hello [Name],"
- Formal: "Dear [Name],"
```

## Execution Reliability Rules (General)

Apply these to any skill that calls CLIs/APIs or writes files:

- Suppress non-essential warnings/noise before JSON parsing (`--only-show-errors` or equivalent) so structured parsing is reliable.
- Prefer simpler, robust data flows over fragile one-liners (for example, fetch full JSON and parse locally when shell escaping is risky).
- Treat empty outputs that are valid business states (for example, zero child items) as success paths, not parse failures.
- Do not emit final success markers (for example, `PASS`) until required artifacts are verified (files created, key fields populated).
- If a critical command fails due sandbox/permission limits, rerun with escalation and continue automatically.
- Include a post-write verification step for generated files (existence, encoding, and line endings).

## PowerShell Implementation Tips (Reusable)

Use these patterns when writing skills that execute PowerShell commands:

- Use single-quoted here-strings (`@' ... '@`) when generating Markdown/YAML templates to prevent accidental variable interpolation.
- When calling CLIs expected to return JSON, capture output as text first, then parse with `ConvertFrom-Json` only after checking for empty output.
- For commands that may return warnings mixed with JSON, suppress warnings (`--only-show-errors`) or extract the JSON payload before parsing.
- Prefer fetching full JSON and selecting fields in PowerShell when JMESPath quoting becomes fragile in PowerShell command strings.
- Treat PowerShell quoting explicitly:
  - Use single quotes for literals whenever possible.
  - Escape embedded single quotes by doubling them (`''`).
  - Avoid complex nested escaping in one-liners; split into steps.
- Use `$LASTEXITCODE` checks immediately after external CLI calls and fail early with clear messages.
- For optional outputs (for example, no child tasks), handle empty strings as valid data states.
- Normalize output files after writes (encoding + line endings) and verify with byte-level checks when correctness matters.

**Robust JSON capture pattern:**

```powershell
$text = (& az boards work-item show --id $storyId --output json --only-show-errors) -join "`n"
if ($LASTEXITCODE -ne 0) {
    throw "Azure CLI command failed"
}
if ([string]::IsNullOrWhiteSpace($text)) {
    throw "Expected JSON output but command returned empty output"
}
$obj = $text | ConvertFrom-Json
```

**Optional JSON output pattern (empty is valid):**

```powershell
$text = (& az boards query --wiql $wiql --output json --only-show-errors) -join "`n"
if ($LASTEXITCODE -ne 0) {
    throw "Task query failed"
}
$items = @()
if (-not [string]::IsNullOrWhiteSpace($text)) {
    $parsed = $text | ConvertFrom-Json
    if ($parsed.workItems) {
        $items = @($parsed.workItems)
    }
}
```

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---------|------------------|
| Saving as UTF-8 with BOM | Save as UTF-8 without BOM |
| Blank line before frontmatter | Start file with `---` on line 1 |
| Naming file `my-skill.md` | Name it `SKILL.md` |
| Forgetting frontmatter | Always include `---` delimited YAML at the top |
| Missing `name` field | Include required frontmatter fields |
| Putting `SKILL.md` directly in skills folder | Create a subfolder first |
| Using lowercase `skill.md` | Use uppercase `SKILL.md` |
| Using `$ARGUMENTS` but no `argument-hint` | Add `argument-hint` whenever the skill accepts arguments |
| `argument-hint` exists but description does not start with `Args:` | Prefix description with `Args: <argument-hint>.` |
| Not documenting required vs optional arguments | Include a Parse Arguments step with fallback behavior |
| Hardcoding values that should be arguments | Use `$ARGUMENTS` for user-provided values |
| Mixed CRLF/LF in generated docs | Normalize line endings and verify no mixed endings remain |
| Parsing JSON with warning text mixed into output | Suppress warnings and parse only structured JSON |
| Declaring success before verifying outputs | Verify required files/fields before emitting success |
| Fragile PowerShell one-liner escaping | Split commands into explicit steps and parse locally |
| Using `ConvertFrom-Json` on empty output | Check for empty output before parsing |

## Verification Checklist

Before considering a skill complete, verify:

- [ ] Folder exists at `$CODEX_HOME/skills/[skill-name]/` or `~/.codex/skills/[skill-name]/`
- [ ] File is named exactly `SKILL.md` (uppercase)
- [ ] Byte 0 is `-` and line 1 is exactly `---`
- [ ] File is UTF-8 without BOM (first 3 bytes are not `EF BB BF`)
- [ ] Frontmatter has `name` and `description`
- [ ] Frontmatter is enclosed in `---` markers
- [ ] Markdown content follows the frontmatter
- [ ] Description includes "Use when..." guidance
- [ ] If the skill accepts arguments: `argument-hint` is in frontmatter
- [ ] If the skill accepts arguments: description starts with `Args: <argument-hint>.`
- [ ] If the skill uses `$ARGUMENTS`: a Parse Arguments step documents positional arguments and fallback behavior
- [ ] If skill writes files: line endings are normalized to repository convention (for example, CRLF on Windows repos)
- [ ] If skill writes files: post-write check confirms no mixed line endings
- [ ] If skill parses CLI/API JSON: warning/noise suppression and empty-output handling are documented
- [ ] If skill uses PowerShell: quoting, `$LASTEXITCODE` checks, and JSON parsing guards are documented
- [ ] Skill does not emit final success until required artifacts are verified

## Quick Reference

```
Location:       $CODEX_HOME/skills/[skill-name]/SKILL.md
                or ~/.codex/skills/[skill-name]/SKILL.md
File name:      SKILL.md (UPPERCASE)
Encoding:       UTF-8 without BOM
Frontmatter:    Start at line 1 with ---
                name, description
                user_invocable (recommended)
                argument-hint (required if skill accepts arguments)
                description starts with Args: <argument-hint>. when args exist
Content:        Markdown instructions after frontmatter
Line endings:   Follow target repo convention; normalize generated files (CRLF for typical Windows repos)
Validation:     Verify required outputs before final success; for CRLF files ensure bare LF count is 0
Arguments:      $ARGUMENTS (full string), $ARGUMENTS[0], $ARGUMENTS[1], etc.
PowerShell:     Prefer stepwise commands, guard JSON parsing, check $LASTEXITCODE after CLI calls
```