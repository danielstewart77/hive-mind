---
name: skill-creator-claude
description: Guide for creating Claude Code skills correctly. Use when creating new skills to ensure proper folder structure and SKILL.md formatting.
model: sonnet
user-invocable: true
---

# How to Create a Claude Code Skill

## Overview

Skills are reusable instructions that extend Claude Code's capabilities. They are stored in the `.claude/skills` directory and consist of a folder containing a `SKILL.md` file.

## Required Structure

### Folder Organization

```
.claude/
└── skills/
    └── your-skill-name/        <-- Folder name = skill name
        └── SKILL.md            <-- Must be named exactly SKILL.md (uppercase)
```

**IMPORTANT:**

- The skill folder name becomes the skill identifier
- The skill file MUST be named `SKILL.md` (all uppercase, with `.md` extension)
- Do NOT name the file after the skill (e.g., `my-skill.md`) - this will NOT work

### SKILL.md File Format

Every SKILL.md file MUST have two parts:

1. **YAML Frontmatter** (required) - metadata between `---` markers
2. **Markdown Content** (required) - the actual skill instructions

## YAML Frontmatter

The frontmatter must include these fields:

```yaml
---
name: your-skill-name
description: A concise description of what this skill does and when to use it.
user-invocable: true
---
```

### Frontmatter Fields

| Field            | Required | Description                                                                                                                             |
| ---------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `name`           | Yes      | The skill identifier (should match folder name)                                                                                         |
| `description`    | Yes      | Brief description shown in skill listings. Include "Use when..." to help Claude know when to apply it.                                  |
| `user-invocable` | Yes      | Set to `true` if users can invoke with `/skill-name`, `false` for background skills                                                     |
| `argument-hint`  | No*      | Placeholder hint shown after `/skill-name` in the CLI (e.g., `[story-number]`). **Always include this if the skill accepts arguments.** |
| `model`          | No       | Force a specific model: `opus`, `sonnet`, or `haiku`                                                                                    |
| `tools`          | No       | Comma-separated list of tools the skill needs (e.g., `Read, Write, Edit, Bash`)                                                         |

\* `argument-hint` is required whenever the skill uses `$ARGUMENTS`.

### Arguments (`$ARGUMENTS`)

When a user invokes a skill with arguments (e.g., `/my-skill 9576 some-name`), those arguments are available in the skill body via the `$ARGUMENTS` variable.

**Syntax:**

| Variable        | Resolves To                            |
| --------------- | -------------------------------------- |
| `$ARGUMENTS`    | The full argument string as-is         |
| `$ARGUMENTS[0]` | First positional argument              |
| `$ARGUMENTS[1]` | Second positional argument             |
| `$ARGUMENTS[N]` | Nth positional argument (zero-indexed) |

**Rules:**

- **Always** add `argument-hint` to the frontmatter when the skill accepts arguments — this tells the user what to type
- In the skill body, document which arguments are expected, which are required vs. optional, and provide fallback behavior (e.g., "If not provided, ask the user")
- Use a dedicated step (e.g., "Step 1: Parse Arguments") to extract and validate arguments early in the workflow

**Example frontmatter with argument-hint:**

```yaml
---
name: get-story
description: Pulls an ADO story and creates local documents. Use when starting work on a new story.
argument-hint: [story-number] [documents-path]
user-invocable: true
---
```

**Example argument parsing in the body:**

```markdown
### Step 1: Parse Arguments

Extract from `$ARGUMENTS`:

- `$ARGUMENTS[0]` = Story number (required, e.g., `9576`)
- `$ARGUMENTS[1]` = Documents path (optional, e.g., `C:\...\documents`)

If the story number is not provided, ask the user.
```

## Markdown Content

After the frontmatter, write your skill instructions in Markdown:

```markdown
---
name: example-skill
description: Example skill demonstrating proper format. Use when learning to create skills.
user-invocable: true
---

# Skill Title

## Overview

Explain what this skill does and its purpose.

## Instructions

Provide clear, actionable instructions for Claude to follow.

## Examples

Include examples of how to use the skill.

## Best Practices

Add any tips or guidelines.
```

## Complete Example

Here's a complete skill that accepts arguments:

**Folder:** `.claude/skills/greeting-helper/SKILL.md`

```markdown
---
name: greeting-helper
description: Helps format professional greetings. Use when the user needs to write a greeting.
argument-hint: [recipient-name] [formality]
user-invocable: true
---

# Greeting Helper

## Overview

This skill helps create professional greetings for emails and messages.

## Process

### Step 1: Parse Arguments

Extract from `$ARGUMENTS`:

- `$ARGUMENTS[0]` = Recipient name (optional — if not provided, ask the user)
- `$ARGUMENTS[1]` = Formality level: `casual`, `professional`, or `formal` (optional — defaults to `professional`)

### Step 2: Generate Greeting

Based on the formality level, generate the greeting:

- Casual: "Hi [Name],"
- Professional: "Hello [Name],"
- Formal: "Dear [Name],"
```

## Common Mistakes to Avoid

| Mistake                                                  | Correct Approach                                                                                           |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Naming file `my-skill.md`                                | Name it `SKILL.md`                                                                                         |
| Forgetting frontmatter                                   | Always include `---` delimited YAML at the top                                                             |
| Missing `name` field                                     | Include all three required frontmatter fields                                                              |
| Putting SKILL.md directly in skills folder               | Create a subfolder first                                                                                   |
| Using lowercase `skill.md`                               | Use uppercase `SKILL.md`                                                                                   |
| Using `$ARGUMENTS` but no `argument-hint`                | Always add `argument-hint` to frontmatter when the skill accepts arguments                                 |
| Not documenting which arguments are required vs optional | Include a "Parse Arguments" step listing each `$ARGUMENTS[N]` with required/optional and fallback behavior |
| Hardcoding values that should be arguments               | Use `$ARGUMENTS` for any value the user should be able to pass at invocation time                          |

## Verification Checklist

Before considering a skill complete, verify:

- [ ] Folder exists at `.claude/skills/[skill-name]/`
- [ ] File is named exactly `SKILL.md` (uppercase)
- [ ] Frontmatter has `name`, `description`, and `user_invocable`
- [ ] Frontmatter is enclosed in `---` markers
- [ ] Markdown content follows the frontmatter
- [ ] Description includes "Use when..." guidance
- [ ] If the skill accepts arguments: `argument-hint` is in the frontmatter
- [ ] If the skill uses `$ARGUMENTS`: a "Parse Arguments" step documents each positional argument (required/optional, example values, fallback behavior)

## Quick Reference

```
Location:       .claude/skills/[skill-name]/SKILL.md
File name:      SKILL.md (UPPERCASE, not skill.md or [name].md)
Frontmatter:    name, description, user_invocable (all required)
                argument-hint (required if skill accepts arguments)
Content:        Markdown instructions after frontmatter
Arguments:      $ARGUMENTS (full string), $ARGUMENTS[0], $ARGUMENTS[1], etc.
```
