# Phase 2E — Plugin Distribution

> **Status:** Not yet implemented. Depends on Phase 2D completion.

---

## Goal

Package all Hive Mind skills and agents into distributable plugins for both Claude Code and Codex. A new user can install the plugin and get all setup, mind management, and operational skills without cloning the full repo.

---

## Scope

1. **Inventory all skills and agents** — produce a complete manifest of every skill and agent in the project
2. **Convert Claude skills to Codex skills** — use the existing Codex skill conversion skill to produce Codex-compatible versions of all Claude skills
3. **Bundle Claude plugin** — package all Claude skills, agents, hooks, and MCP config into a Claude Code plugin (`hivemind` plugin)
4. **Bundle Codex plugin** — package all converted Codex skills into a Codex plugin
5. **Distribution** — publish via npm/GitHub for Claude, appropriate channel for Codex

---

## Step 1: Skill and Agent Inventory

Produce a complete manifest of everything that needs to be bundled:

**Skills** (from `.claude/skills/`):
- Setup: `setup`, `setup-prerequisites`, `setup-config`, `setup-auth`, `setup-nervous-system`, `setup-provider`, `setup-body`, `setup-mind`
- Mind CRUD: `add-mind`, `create-mind`, `update-mind`, `remove-mind`, `list-minds`
- Provider CRUD: `add-provider`, `update-provider`, `remove-provider`
- Operations: `generate-compose`, `export-config`
- Communication: `send-message-to-mind`, `moderate`
- Development: `planning-genius`, `code-genius`, `code-review-genius`, `master-code-review`, `tool-creator`, `mcp-tool-builder`
- Memory: `remember`, `save-session`, `memory-manager`, `semantic-memory-save`, `knowledge-graph-save`, `pin-memory-action`, `notify-action`, `create-data-class`
- System: `sitrep`, `secrets`, `reminders`, `check-reminders`, `notify`, `agent-logs`
- Identity: `self-reflect`, `seed-mind`
- Workflow: `orchestrator`, `story-start`, `story-close`, `design-session`, `commit`
- Content: `post-to-linkedin`, `convert-to-pdf`, `pdf-formatter`, `mermaid-diagram-creator`, `update-documentation`
- External: `browse`, `weather`, `crypto-price`, `x-ai-lurker`, `x-search`, `send-email`, `planka`, `current-time`
- Meta: `skill-creator-claude`, `create-agents-claude`, `create-story`
- Scheduling: `schedule`, `loop`, `3am`, `7am`, `1pm`, `morning-briefing`, `remind-me`

**Agents** (from `.claude/agents/`):
- `poll-task-result`
- Any other agents in the directory

**Hooks** (from `.claude/settings.json`):
- SessionStart, Stop hooks

**MCP config:**
- `.mcp.json` (template — paths will need adaptation per install)

## Step 2: Convert Claude Skills to Codex Skills

Use the existing Codex skill conversion skill to transform all Claude SKILL.md files into Codex-compatible format.

- [ ] Identify the conversion skill (Daniel's recently created Codex conversion skill)
- [ ] Run it against every Claude skill to produce Codex equivalents
- [ ] Review converted skills for accuracy — some skills reference Claude-specific tools or features that may need manual adjustment
- [ ] Handle skills that use Claude-only features (e.g., `context: fork`, specific agent types) — either adapt or mark as Claude-only

Codex skill differences to account for:
- Different tool names/capabilities
- Different permission model
- Different subagent system
- Different MCP integration patterns

## Step 3: Bundle Claude Plugin

Create the Claude Code plugin structure:

```
hivemind-plugin/
├── .claude-plugin/
│   └── plugin.json           # name, version, description, author
├── skills/
│   ├── setup/SKILL.md
│   ├── setup-prerequisites/SKILL.md
│   ├── add-mind/SKILL.md
│   ├── ... (all skills)
├── agents/
│   ├── poll-task-result.md
│   └── ... (all agents)
├── hooks/
│   └── hooks.json
├── .mcp.json                  # template MCP config
├── settings.json              # default settings
└── README.md
```

**Plugin manifest (`plugin.json`):**
```json
{
  "name": "hivemind",
  "version": "1.0.0",
  "description": "Hive Mind — multi-mind AI orchestration system",
  "author": "Daniel Stewart",
  "skills": true,
  "agents": true,
  "hooks": true
}
```

- [ ] Create plugin directory structure
- [ ] Copy all skills into `skills/`
- [ ] Copy all agents into `agents/`
- [ ] Extract hooks into `hooks/hooks.json`
- [ ] Create template `.mcp.json` with placeholder paths
- [ ] Write README with installation instructions
- [ ] Test: `claude --plugin-dir ./hivemind-plugin` — verify all skills appear as `/hivemind:<skill-name>`

## Step 4: Bundle Codex Plugin

Create a native Codex plugin. Codex supports SKILL.md markdown files with the same format as Claude Code, but with simpler frontmatter (only `name` and `description` — no `user-invocable`, `allowed-tools`, etc.).

**Note:** The `openai/codex-plugin-cc` research in `plans/ollama-plugin.md` is about a Codex plugin FOR Claude Code (provider delegation). That is unrelated to this step. This is a native Codex plugin used BY Codex.

**Codex plugin structure:**
```
hivemind-codex-plugin/
├── .codex-plugin/
│   └── plugin.json           # manifest (name, version, description)
├── skills/
│   ├── setup/SKILL.md
│   ├── add-mind/SKILL.md
│   ├── ... (all converted skills)
├── .mcp.json                  # template MCP config
└── README.md
```

**Codex vs Claude skill differences:**

| Aspect | Claude Code | Codex |
|---|---|---|
| Frontmatter | Extended (`user-invocable`, `allowed-tools`, `model`, `context`) | Minimal (`name`, `description` only) |
| Skill location | `.claude/skills/` | `.agents/skills/` (repo) or `~/.agents/skills` (user) |
| Agent location | `.claude/agents/` | `~/.codex/agents/` or `.codex/agents/` |
| Config format | JSON (`.claude/settings.json`) | TOML (`~/.codex/config.toml`) |
| Invocation control | `user-invocable: false` restricts to model-only | All skills are user+model invocable |
| Plugin manifest | `.claude-plugin/plugin.json` | `.codex-plugin/plugin.json` |
| Plugin install | `claude /plugin marketplace add <name>` | `/plugins` command or Codex app UI |
| Marketplace | npm / GitHub | marketplace.json (repo-scoped or user-level) or OpenAI curated |

**Conversion process:**

- [ ] For each Claude skill, create a Codex version:
  - Strip unsupported frontmatter fields (`user-invocable`, `allowed-tools`, `model`, `context`, `tools`, `argument-hint`)
  - Keep only `name` and `description` in frontmatter
  - Body content (steps, bash commands, instructions) stays the same
  - Review for Claude-specific tool references that may need adaptation
- [ ] Create `.codex-plugin/plugin.json` manifest
- [ ] Adapt agents for Codex subagent system (Codex uses `~/.codex/agents/`, supports `default`, `worker`, `explorer` types)
- [ ] Create template `.mcp.json` with placeholder paths
- [ ] Write README with Codex installation instructions
- [ ] Test: install plugin via Codex `/plugins` command, verify skills load
- [ ] Register in a marketplace.json for distribution

## Step 5: Distribution

### Three GitHub repos

| Repo | Plugin | What it contains |
|---|---|---|
| `danielstewart77/hivemind-plugin` | Hive Mind for Claude Code | `.claude-plugin/`, all skills, agents, hooks, `.mcp.json` template |
| `danielstewart77/hivemind-codex-plugin` | Hive Mind for Codex | `.codex-plugin/`, converted skills, agents, `.mcp.json` template, `.agents/plugins/marketplace.json` |
| `danielstewart77/ollama-plugin` | Ollama delegation for Claude Code | `.claude-plugin/`, ollama skills/agents, broker daemon |

Each repo is independent. The Hive Mind plugins point users to clone the `hive_mind` server repo during `/setup`. The Ollama plugin is fully standalone.

### Install paths

**Hive Mind (Claude):**
```
claude /plugin marketplace add danielstewart77/hivemind-plugin
/setup all
```

**Hive Mind (Codex):**
```
# Clone or add marketplace entry
/plugins → search hivemind → install
/setup all
```

**Ollama (Claude):**
```
claude /plugin marketplace add danielstewart77/ollama-plugin
```

### Official marketplace submission

Submit all three to their respective official marketplaces in parallel with the GitHub distribution:

| Plugin | Official submission | Interim distribution |
|---|---|---|
| hivemind-plugin | [claude.ai/settings/plugins/submit](https://claude.ai/settings/plugins/submit) | GitHub repo (auto-discovered) |
| hivemind-codex-plugin | [platform.openai.com/apps-manage](https://platform.openai.com/apps-manage) (when self-serve opens) | marketplace.json in repo + list on [awesome-codex-plugins](https://github.com/hashgraph-online/awesome-codex-plugins) |
| ollama-plugin | [claude.ai/settings/plugins/submit](https://claude.ai/settings/plugins/submit) | GitHub repo (auto-discovered) |

**Claude marketplace** is open now — submit and wait for review. No published timeline for approval.

**Codex marketplace** self-serve is "coming soon." In the meantime, distribute via marketplace.json in the repo and register on the awesome-codex-plugins community list.

### Checklist

- [ ] Create `danielstewart77/hivemind-plugin` repo with Claude plugin structure
- [ ] Create `danielstewart77/hivemind-codex-plugin` repo with Codex plugin structure + marketplace.json
- [ ] Create `danielstewart77/ollama-plugin` repo with Claude plugin structure + broker daemon
- [ ] Submit hivemind-plugin to Claude official marketplace
- [ ] Submit ollama-plugin to Claude official marketplace
- [ ] Register hivemind-codex-plugin on awesome-codex-plugins
- [ ] Submit hivemind-codex-plugin to Codex curated directory when self-serve opens
- [ ] Publish pre-built Docker images to Docker Hub (optional)
- [ ] Version strategy: plugin versions track the main hive_mind version
- [ ] Each repo has README with install instructions, screenshots, and link to server repo

---

## Notes

- The plugin is the distribution mechanism for skills/agents/hooks. The server code (gateway, broker, MCP) lives in the `hive_mind` repo and is cloned during `/setup`.
- A user installing the plugin gets all the `/setup` skills, which guide them through deploying the server infrastructure.
- Some skills are only meaningful inside the hive_mind project (e.g., `orchestrator`, `story-start`). They can still be bundled — they'll just do nothing if the project structure isn't present.
- The Ollama plugin is fully standalone — no Hive Mind dependency. Any Claude Code user with an Ollama endpoint can use it.
