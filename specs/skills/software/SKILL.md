---
name: software
description: Route all software development requests — code, architecture, debugging, building tools, documentation. Use for any request about the codebase, system design, creating tools or skills, or the dev story pipeline.
user-invocable: true
---

# Software

**Step 1 — Announce**

> *Using: software.*

**Step 2 — Route to the right skill**

Pick the skill that matches the request and invoke it.

### Dev Lifecycle — story pipeline, coding, review

| Skill | When to use |
|---|---|
| `create-story` | Creating a new Planka story card |
| `story-start` | Kicking off work on an existing story |
| `planning-genius` | Generating a TDD implementation plan |
| `code-genius` | Writing code from a plan |
| `code-review-genius` | Reviewing code against requirements |
| `story-close` | Closing a completed story after merge |
| `orchestrator` | Running the full dev pipeline end-to-end |
| `design-session` | Architecture or design discussion before coding |

### Tooling — building tools, skills, agents, docs

| Skill | When to use |
|---|---|
| `tool-creator` | Creating a new stateless or stateful Hive Mind tool |
| `skill-creator-claude` | Creating a Claude Code skill |
| `create-agents-claude` | Creating a Claude Code subagent |
| `convert-claude-skill-to-codex` | Porting a Claude skill to Codex format |
| `mermaid-diagram-creator` | Creating or fixing Mermaid diagrams |
| `update-documentation` | Updating README and linked docs |
| `master-code-review` | Security-aware code review with framework detection |

**If the request is informational** ("how does X work", "what does Y do", "where is Z"): read the relevant code and specs first, then answer. No sub-skill needed — but always read before answering.
