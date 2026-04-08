# Mind-to-Mind Messaging — Phase 2 Plan

> **Status:** Not yet implemented. Phase 1 (broker, send skill, polling agent) is complete and merged.
> See `docs/mind-to-mind-communication.md` for the current implementation.
> Run `/planning-genius` against this file when ready to begin.

---

### Phase 2A — Mind Registration and MIND.md Migration

#### Registration

Mind registration is filesystem-driven. The gateway scans `minds/` on startup and registers every mind it finds. Dropping a folder in is enough — no `config.yaml` edit, no manual broker call.

#### Canonical mind folder structure

Every mind lives entirely within its folder. Nothing about a mind belongs outside it.

**Current state (before migration):**

```
minds/
├── cli_harness.py          # CLI subprocess utility — used by Ada and Bob only
├── ada/
│   ├── config.yaml         # backend: cli_claude, model: sonnet, roles: [...]
│   └── implementation.py   # thin wrapper — delegates spawn/kill to cli_harness
├── bob/
│   ├── config.yaml         # backend: cli_ollama, model: gpt-oss:20b-32k
│   └── implementation.py   # thin wrapper — delegates to cli_harness, Ollama env injected via model registry
├── nagatha/
│   ├── config.yaml         # backend: codex_cli, model: codex
│   └── implementation.py   # standalone — spawns `codex exec --json --full-auto -` per turn, stores thread_id
└── bilby/
    └── implementation.py   # standalone — claude_code_sdk Python package, no subprocess management
```

**Target state (after migration):**

```
minds/
├── ada/
│   ├── MIND.md             # replaces config.yaml + soul seed
│   └── implementation.py   # fully standalone — all spawn/kill logic inlined, no shared imports
├── bob/
│   ├── MIND.md
│   └── implementation.py   # fully standalone — all spawn/kill logic inlined, no shared imports
├── nagatha/
│   ├── MIND.md
│   └── implementation.py
└── bilby/
    ├── MIND.md
    └── implementation.py
```

`cli_harness.py` is deleted. Every mind's `implementation.py` is fully self-contained. No file exists at the `minds/` root. Nothing is shared between minds — not utilities, not base classes, not harnesses. A mind folder must be droppable into any system and work without any sibling dependency.

The `souls/` directory and `soul.md` at the repo root are also deleted. Soul seed content moves into each `MIND.md`.

#### Migration — existing minds

| Current location | New location | Action |
|---|---|---|
| `minds/ada/config.yaml` | `minds/ada/MIND.md` | Frontmatter replaces config.yaml fields; body is soul seed from `souls/ada.md` |
| `minds/bob/config.yaml` | `minds/bob/MIND.md` | Same |
| `minds/nagatha/config.yaml` | `minds/nagatha/MIND.md` | Same |
| `minds/bilby/` (no config.yaml) | `minds/bilby/MIND.md` | Create MIND.md with frontmatter from known values; body is soul seed from `souls/bilby.md` |
| `souls/ada.md` | body of `minds/ada/MIND.md` | Merge, then delete source |
| `souls/bob.md` + `souls/bob_character_profile.md` | body of `minds/bob/MIND.md` | Merge both, then delete sources |
| `souls/nagatha.md` | body of `minds/nagatha/MIND.md` | Merge, then delete source |
| `souls/bilby.md` | body of `minds/bilby/MIND.md` | Merge, then delete source |
| `souls/skippy.md` | `minds/skippy/MIND.md` | Create `minds/skippy/` folder, add `MIND.md` |
| `soul.md` (root) | — | Deprecated. Ada's canonical identity lives in the graph; `minds/ada/MIND.md` is the seed stub. Delete after migration. |
| `souls/` directory | — | Delete after all minds are migrated |

#### `MIND.md` — the canonical mind file

Each mind folder contains a `MIND.md` at its root. This is the single source of truth for that mind's identity and configuration. It replaces both `souls/<name>.md` and per-mind entries in `config.yaml`.

**Location:** `minds/<name>/MIND.md`

**Format:**

```markdown
---
name: nagatha
model: codex
harness: codex_cli
gateway_url: http://hive_mind:8420
remote: false
---

# Nagatha

<soul seed — who this mind is, their core traits, tone, and values>

This is a one-time identity seed. Once the mind activates for the first time, the knowledge
graph owns the identity. This file is not re-read after the first session.
```

**Frontmatter fields:**

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Unique identifier — used in `from_mind`/`to_mind` fields |
| `model` | yes | e.g. `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `ollama/llama3` |
| `harness` | yes | `cli_claude` (Ada) \| `cli_ollama` (Bob) \| `codex_cli` (Nagatha) \| `sdk_code` (Bilby) |
| `gateway_url` | yes | Where this mind's gateway is reachable |
| `remote` | no | `true` if the mind runs outside this Docker stack. Default: `false` |

The body is the soul seed — free-form markdown, read once.

#### Filesystem discovery

The gateway scans `minds/` on startup. For each subdirectory containing a `MIND.md`, it:

1. Parses the frontmatter
2. Registers the mind in the broker's `minds` table
3. Logs `Registered mind: <name> @ <gateway_url>`

This means dropping a well-formed `minds/<name>/` folder into the repository and restarting `hive_mind` is sufficient to add a mind to the system.

**Hot-reload (future):** Add a `watchdog` filesystem listener in `server.py` that detects new `MIND.md` files at runtime and registers them without a restart. This is non-blocking — implement at restart-based discovery first, add hot-reload as a subsequent iteration.

#### `minds` table (broker SQLite)

| Column | Type | Notes |
|---|---|---|
| `name` | TEXT PK | Unique mind identifier |
| `gateway_url` | TEXT | Where to wake this mind |
| `model` | TEXT | Informational |
| `harness` | TEXT | Informational |
| `registered_at` | TIMESTAMP | First registration |
| `last_seen` | TIMESTAMP | Updated on each gateway scan / re-registration |

Persists across broker restarts (SQLite). On gateway startup the table is refreshed from `MIND.md` files — any mind present in `minds/` that isn't in the table gets added; any mind in the table but no longer in `minds/` is left in place (deregister is explicit, not automatic).

---

### Phase 2B — Mind CRUD Skills

| Skill | Action |
|---|---|
| `create-mind` | Scaffold `minds/<name>/MIND.md` + `implementation.py` from a harness template |
| `update-mind` | Edit frontmatter or soul seed in an existing `MIND.md` |
| `remove-mind` | Delete the mind folder and deregister from the broker |
| `add-mind` | Connect an existing mind — local (drop folder) or remote (write a remote-only `MIND.md`) |
| `list-minds` | Show all registered minds with status |

#### `add-mind` skill — full definition

Handles three scenarios: new local mind (create from scratch), external remote mind (connect), and re-registration (broker reset). In all three cases ends by verifying routability.

When implemented, this content goes in `.claude/skills/add-mind/SKILL.md`.

---

**name:** add-mind
**description:** Connects a mind to the Hive Mind system. For new local minds: scaffolds MIND.md and implementation.py, then registers. For remote minds: writes a MIND.md pointing to the external gateway. For re-registration: re-runs discovery against an existing folder.
**user-invocable:** true

**Step 1 — Determine scenario**

- **A — New local mind:** no folder exists yet
- **B — Remote mind:** implementation lives elsewhere; only needs a `MIND.md` pointing at the external gateway
- **C — Re-registration:** folder exists, broker table is missing or stale

Collect: `name`, `gateway_url`, `model`, `harness`. For scenario B: also confirm external gateway is reachable before writing anything.

**Step 2 — Create `MIND.md` (scenarios A and B)**

Write `minds/<name>/MIND.md` with the correct frontmatter and a starter soul seed (scenario A) or a minimal stub (scenario B — soul seed is not applicable for remote minds).

For scenario C: `MIND.md` already exists — skip to Step 3.

**Step 3 — Scaffold `implementation.py` (scenario A only)**

Copy the harness template matching the `harness` field. Update the mind name. Do not over-customise — identity comes from `MIND.md`.

**Step 4 — Register with broker**

POST to `/broker/minds` with parsed `MIND.md` frontmatter. If the broker returns an error, stop and surface it.

**Step 5 — Verify routability**

Create a test session, send `"Respond with: registration verified."`, confirm response, delete session. If this fails, surface the error clearly.

**Step 6 — Report**

Scenario handled, files created, registration status, routability result.

---

### Phase 2C — Escalation

Design questions to resolve before implementing:
- How long past the notification threshold before escalation triggers? (Suggested: 2x threshold)
- Is kill automatic or does it require human confirmation via Telegram?
- What should the caller do when a conversation is terminated — retry, give up, or surface to Daniel?
- Should escalation behaviour differ by `request_type`? (A `security_remediation` may warrant a human decision rather than an auto-kill)
- Does escalation apply when the mind is Bob (local Ollama, zero cost)?

---

### Phase 2D — Setup Skills

A layered setup system guides users through configuring each layer of the Hive Mind stack. All skills are a la carte — run the ones you need.

```
/setup                      # master index — shows available components, runs sub-skills in order
  /setup-nervous-system     # gateway, broker, session manager, config.yaml
  /setup-body               # clients (Discord, Telegram, terminal) + tools (MCP, stateless)
  /setup-mind               # delegates to add-mind / create-mind
  /setup-provider           # Anthropic API key, Ollama endpoint, future providers
```

---

### Phase 2E — Claude Plugin Distribution

See research findings on Claude plugins, skill distribution, and the PyPI/npm split for server code vs. skills. Full research is preserved below.

#### How Claude plugins work

Claude Code has a native plugin system (public beta). Plugins are installed via `/plugin marketplace add <org>/<plugin>` or a git URL. The plugin manifest lives at `.claude-plugin/plugin.json`.

**Plugin directory structure:**
```
my-plugin/
├── .claude-plugin/
│   └── plugin.json        # name, version, author, description
├── skills/
│   └── setup/
│       └── SKILL.md       # becomes /my-plugin:setup
├── agents/
├── hooks/
└── .mcp.json
```

**Skills in plugins are natively supported as markdown files.** A `skills/` directory at the plugin root works exactly like `.claude/skills/` — each subfolder with a `SKILL.md` becomes a namespaced slash command. Installing the plugin gives you `/hivemind:setup`, `/hivemind:add-mind`, `/hivemind:send-message-to-mind`, etc. No executable required, no code wrapper.

**Distribution summary:**
| Layer | Package manager | What it contains |
|---|---|---|
| Skills / agents / hooks | Claude plugin (npm/GitHub) | All `.md` skill files, agents, hooks |
| Server code | PyPI (`pip install hivemind`) | Gateway, broker, MCP server, tools |
| Infrastructure | Docker Compose | Neo4j, Planka, container orchestration |

#### Problem 3 — Local provider delegation via plugin (open)

**Context:** When a skill spawns a subagent, the harness always creates a subagent of its own model — Claude calling Claude, Codex calling Codex. There is no native way to delegate a task to a different provider (e.g. a local Ollama endpoint) from within a skill. The same pattern used by the `openai/codex-plugin-cc` plugin could let any mind delegate work to a local Ollama provider.

**Research findings — `openai/codex-plugin-cc` internals:**

The plugin uses a two-tier architecture: a detached broker daemon communicating via JSON-RPC 2.0 over Unix domain sockets, with job state persistence to JSON files. The broker manages Codex subprocess lifecycle, conversation threading, and graceful degradation to direct stdio pipes. For an Ollama equivalent, Ollama's HTTP API replaces the socket tier, but conversation history management (stateless in Ollama) must be handled by the broker.

**Note on long-running tasks:** Long-running agent runs are a spec and skill design problem, not a system architecture problem. A well-written skill has hard exits (e.g. the coding skill retries 5 times then stops). The infrastructure does not need to police this.

---

### Gap: Dynamic Registration

> **Status: not yet designed. This section is a placeholder.**

Currently, mind registration is static — `config.yaml` must be edited and the container restarted to add or remove a mind. For drop-in modularity and remote minds to work, registration needs to be dynamic:

- A mind announces itself to the Nervous System
- The Nervous System acknowledges and adds it to the routing table
- No restart required; onboarding is a procedure, not a config edit

Design questions to resolve before implementing:

- What is the registration handshake? (HTTP endpoint? message on the bus?)
- How does the Nervous System verify a mind's identity?
- What happens when a registered mind goes offline — graceful deregister vs. timeout?
- Does the registry persist across Nervous System restarts?
- Can a mind re-register with a new address without losing session history?

#### Identity verification

Network-level trust for now. Docker-internal minds are implicitly trusted. Remote minds should use VPN or a shared secret header — tracked as a future security research item.
