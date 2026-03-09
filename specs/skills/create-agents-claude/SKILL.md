---
name: create-agents-claude
description: Guide for creating Claude Code subagents correctly. Use when the user wants to create a custom subagent to ensure proper file structure and frontmatter formatting.
argument-hint: "[description]"
user-invocable: true
model: sonnet
---

# How to Create a Claude Code Subagent

## Lean Subagent Principle

**Every subagent must be lean and minimal.** The body is the system prompt — include only what the agent needs to do its job. No redundant examples, no verbose explanations. Grant only the tools required. A subagent should never need to be trimmed down after creation.

## Required Structure

```
.claude/agents/
└── my-agent.md              <-- Project scope (check into VCS)

~/.claude/agents/
└── my-agent.md              <-- User scope (all projects)
```

- Each file is one subagent — markdown with YAML frontmatter
- The filename (minus `.md`) becomes the agent identifier
- Project-level agents take priority over user-level agents with the same name
- Subagents load at session start; restart session or run `/agents` after creating

## Subagent File Format

Every subagent file has two parts: YAML frontmatter between `---` markers, then the system prompt as markdown content.

### Frontmatter Fields

| Field              | Required | Description                                                                       |
| ------------------ | -------- | --------------------------------------------------------------------------------- |
| `name`             | Yes      | Agent identifier, lowercase + hyphens                                             |
| `description`      | Yes      | Brief description — Claude matches tasks to this                                  |
| `tools`            | No       | Comma-separated tool list. Inherits all if omitted                                |
| `disallowedTools`  | No       | Deny specific tools (e.g., `Write, Bash`)                                         |
| `model`            | No       | `sonnet`, `opus`, `haiku`, or `inherit` (default: `inherit`)                      |
| `maxTurns`         | No       | Max agentic turns before stopping                                                 |
| `permissionMode`   | No       | `default`, `acceptEdits`, `dontAsk`, `bypassPermissions`, `plan`                  |
| `skills`           | No       | YAML list of skill names to preload (not inherited from parent)                   |
| `memory`           | No       | `user`, `project`, or `local` — enables persistent cross-session memory           |
| `hooks`            | No       | Lifecycle hooks scoped to this subagent (same format as settings.json hooks)       |

### Available Tools

`Read`, `Grep`, `Glob`, `Write`, `Edit`, `Bash`, `WebFetch`, `WebSearch`, `AskUserQuestion`, `Skill`, `NotebookEdit`, `TaskCreate`, `TaskUpdate`, `TaskList`, `TaskGet`, `Task`

Restrict spawnable subagents with `Task(worker, researcher)` syntax. Omit `Task` entirely to prevent spawning.

### Memory Scopes

| Scope     | Location                                  | Shareable   |
| --------- | ----------------------------------------- | ----------- |
| `user`    | `~/.claude/agent-memory/<name>/`          | No          |
| `project` | `.claude/agent-memory/<name>/`            | Yes (VCS)   |
| `local`   | `.claude/agent-memory-local/<name>/`      | No          |

When memory is enabled, Read/Write/Edit tools are auto-added and MEMORY.md (first 200 lines) is injected into the system prompt.

## Key Constraints

- Subagents **cannot** spawn other subagents (use skills or chaining instead)
- One purpose per subagent — focused beats general
- Minimal tools — grant only what's needed

## Example

```markdown
---
name: story-pipeline
description: Automated story implementation. Use when given story numbers to implement.
tools: Read, Write, Edit, Glob, Grep, Bash, Skill, AskUserQuestion
model: opus
memory: user
skills:
  - get-story
  - planning-genius
  - code-genius
  - code-review-genius
---

You implement stories end-to-end. When given a story number:

1. Use /get-story to pull story details
2. Use /planning-genius to create an implementation plan
3. Use /code-genius to implement with TDD
4. Use /code-review-genius to self-review
5. Notify when complete

Follow existing codebase patterns.
```
