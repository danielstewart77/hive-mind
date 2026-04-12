---
name: create-mind
description: Create a new mind from a template. Scaffolds MIND.md and implementation.py, then registers with the broker. Use when the user wants to add a brand-new mind to the system.
argument-hint: "[name]"
---

# create-mind

`$ARGUMENTS[0]` = mind name. Ask if missing.

## Step 1 — Gather info

- Mind name from argument
- Ask user for: model to use, brief identity/role description for the soul seed

## Step 2 — Select template

List available templates:
```bash
ls mind_templates/*.py
```

Present choices to the user. Templates marked `# UNTESTED` should be flagged with a warning.

Available permutations:
- `claude_cli_claude` — Claude CLI + Claude models (tested)
- `claude_cli_ollama` — Claude CLI + Ollama models (tested)
- `claude_sdk_claude` — Claude SDK + Claude models (tested)
- `claude_sdk_ollama` — Claude SDK + Ollama models (untested)
- `codex_cli_codex` — Codex CLI + Codex models (tested)
- `codex_cli_ollama` — Codex CLI + Ollama models (untested)
- `codex_sdk_codex` — Codex SDK + Codex models (untested)
- `codex_sdk_ollama` — Codex SDK + Ollama models (untested)

## Step 3 — Scaffold files

```bash
mkdir -p minds/<name>
cp mind_templates/<selected>.py minds/<name>/implementation.py
sed -i 's/MIND_NAME/<name>/g' minds/<name>/implementation.py
touch minds/<name>/__init__.py
```

Write `minds/<name>/MIND.md` with frontmatter from user's choices and soul seed from their description.

## Step 4 — Container isolation (optional)

Ask the user: "Should this mind run in its own isolated container?"

If yes:
- Ask which host directories the mind should access (and read-only vs read-write)
- Ask which secrets the mind needs
- Write the `container:` block into the MIND.md frontmatter
- Note: `/add-mind` will call `/generate-compose` to update docker-compose.yml

If no:
- The mind runs as a subprocess inside the main hive_mind container (default)

## Step 5 — Register

Delegate to `/add-mind <name>` (it will detect Scenario C — directory exists — and handle compose generation, registration, and routability check).

## Step 5 — Report

- Template used
- Files created
- Registration and routability status
