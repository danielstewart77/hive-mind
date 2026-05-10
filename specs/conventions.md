# Hive Mind Conventions

Read this before writing any code, skill, or tool. These are the ordering rules that govern how new capabilities are built.

## The Build Order

Before writing code, ask: can this be done without code?

```
1. CLI / bash chain       — grep, git, curl, jq, etc. No Python needed.
2. Existing skill         — check .claude/skills/ before building anything new.
3. New skill (markdown)   — if the logic is reusable, write a SKILL.md first.
4. New spec (markdown)    — if the behavior requires nuance or decision criteria.
5. Python code (last)     — only if steps 1–4 cannot handle it.
```

Code is the option of last resort. Skills and specs are cheaper to change, easier to reason about, and don't require container restarts.

## The Architecture Pattern

See `specs/hive-mind-architecture.md` for the full pattern. Short version:

- **Events** invoke skills
- **Skills** (markdown) contain the orchestration logic; they read specs for nuance
- **Specs** (markdown) define decision criteria, rules, data shapes — anything that would otherwise be hardcoded
- **Tools** (Python `@tool()`) are pure CRUD — read from or write to a system, no reasoning

Anti-pattern: logic in Python code that should live in a spec.

## When Writing a Skill

Use the `skill-creator-claude` skill to ensure correct folder structure and SKILL.md frontmatter.

```
/skill-creator-claude
```

Skills live in `.claude/skills/<skill-name>/SKILL.md`.

## When Writing a Tool

Use the `tool-creator` skill. It handles the full lifecycle: dependencies, secrets, code generation, registration, smoke testing.

```
/tool-creator <description>
```

Tool rules (enforced by the builder skill):
- Return raw data (JSON strings preferred) — never format for display
- Read secrets via `get_credential(key)` from `tools/secret_manager.py`
- No module-level side effects (no DB connections at import time)
- Catch specific exceptions; return `{"error": "brief description"}` on failure
- All `subprocess.run` calls must use list arguments (`shell=False`)

## Security

Read `specs/security.md` before implementing anything that:
- Touches secrets, credentials, or API keys
- Executes code, shell commands, or subprocesses
- Writes to the filesystem outside the project directory
- Makes outbound network calls with user-supplied URLs

## Branch Naming

See `specs/branching.md`. Short version: `story/*` (orchestrator), `feature/*`, `fix/*`, `refactor/*`.
