# Multi-Mind Architecture

The architectural specification for the Hive Mind multi-mind system. Covers how minds are defined, registered, managed, isolated, secured, and how they communicate.

> **Phase 1** (inter-mind messaging): Complete.
> **Phase 2A** (MIND.md migration): Complete.
> **Phase 2B** (mind CRUD): Complete.
> **Phase 2C** (setup & onboarding): Complete.
> **Phase 2D** (container isolation): Complete. All 4 minds running in isolated containers.
> **Phase 2E** (plugin distribution): See `plans/phase2e-plugin-distribution.md`.

---

## Container Topology — Three Layers

The system is divided into exactly three layers.

```
Nervous System  ──  Minds  ──  Body
```

### Nervous System

Everything required to coordinate minds. Deployed as a single container (`hive_mind`).

| Component | Role |
|---|---|
| `server.py` | FastAPI gateway — routes `mind_id` to each mind's `gateway_url` |
| `core/sessions.py` | Session manager — process pool, lifecycle |
| `core/broker.py` | Message broker — async inter-mind messaging |
| `core/mind_registry.py` | Mind registry — filesystem discovery, in-memory registry |
| SQLite | NS-owned persistence (sessions, broker state, secret scoping) |
| Neo4j | Shared memory and knowledge graph |
| `hive-mind-tools` MCP | Internal tool server — Neo4j memory, graph, browser, inter-mind delegation |

### Minds

Each mind is configured at setup time via `/add-mind`. In container isolation mode, each mind runs in its own container with scoped filesystem access. A mind container runs the same `server.py` image in `MIND_ID=<name>` mode.

### Body

Thin, stateless surfaces connecting the system to the outside world.

| Component | Role |
|---|---|
| Discord Bot | `clients/discord_bot.py` |
| Telegram Bot | `clients/telegram_bot.py` |
| Scheduler | `clients/scheduler.py` — cron daemon |
| `hive-mind-mcp` | External tool server — Gmail, Calendar, HITL, Docker ops |

---

## MIND.md — Canonical Mind Definition

Each mind folder contains a `MIND.md` at its root. This is the single source of truth for that mind's identity and configuration.

**Location:** `minds/<name>/MIND.md`

**Format:**

```markdown
---
name: <name>
model: <model>
harness: <harness>
gateway_url: <url>
remote: false
container:
  image: hive_mind:latest
  volumes:
    - /host/path:/container/path:ro
  environment:
    - CUSTOM_VAR=value
---

# <Name>

<Soul seed — one-time identity bootstrap. The knowledge graph owns identity after first activation.>
```

**Frontmatter fields:**

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Unique identifier |
| `model` | yes | e.g. `claude-sonnet-4-6`, `ollama/llama3` |
| `harness` | yes | e.g. `claude_cli_claude`, `codex_cli_codex` |
| `gateway_url` | yes | Where this mind's gateway is reachable |
| `remote` | no | `true` if outside this Docker stack. Default: `false` |
| `container` | no | Per-mind container config. Omit to run inside the main container. |
| `container.image` | no | Default: `hive_mind:latest` |
| `container.volumes` | no | Host:container[:mode] mount strings |
| `container.environment` | no | Additional env vars |

---

## Mind Registration

### Filesystem Discovery

The gateway scans `minds/` on startup. For each subdirectory containing a `MIND.md`, it:

1. Parses the frontmatter
2. Populates an in-memory mind registry (`mind_id → MindInfo`)
3. Registers the mind in the broker's `minds` table
4. Logs `Registered mind: <name> @ <gateway_url>`

`sessions.py` reads from the in-memory registry — the `config.yaml` `minds:` block has been removed.

### Broker `minds` Table

| Column | Type | Notes |
|---|---|---|
| `name` | TEXT PK | Unique mind identifier |
| `gateway_url` | TEXT | Where to wake this mind |
| `model` | TEXT | Informational |
| `harness` | TEXT | Informational |
| `registered_at` | TIMESTAMP | First registration |
| `last_seen` | TIMESTAMP | Updated on each scan |

### Mind Management Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/broker/minds` | List all registered minds |
| `POST` | `/broker/minds` | Register a mind |
| `PUT` | `/broker/minds/{name}` | Update mind fields (partial) |
| `DELETE` | `/broker/minds/{name}` | Deregister a mind |

### Mind CRUD Skills

| Skill | Action |
|---|---|
| `/create-mind` | Scaffold from harness template, register |
| `/add-mind` | Connect existing mind (local, remote, or re-register). Calls `/generate-compose` if containerised. |
| `/update-mind` | Edit MIND.md frontmatter + broker |
| `/remove-mind` | Deregister, stop container, optional cleanup |
| `/list-minds` | Show all registered minds |

---

## Harness Templates

Templates in `mind_templates/`, named `{harness}_{implementation}_{model-family}.py`:

| Template | Tested? |
|---|---|
| `claude_cli_claude.py` | Yes |
| `claude_cli_ollama.py` | Yes |
| `claude_sdk_claude.py` | Yes |
| `claude_sdk_ollama.py` | No |
| `codex_cli_codex.py` | Yes |
| `codex_cli_ollama.py` | No |
| `codex_sdk_codex.py` | No |
| `codex_sdk_ollama.py` | No |

Each mind's `implementation.py` is fully self-contained — no shared imports between minds.

---

## Container Isolation

### Mind Containers

A mind container is a sandboxed environment — not a cloned nervous system. It contains:
- `mind_server.py` — a minimal HTTP server (~100 lines) that manages the harness subprocess
- The mind's `implementation.py` — loaded by mind_server.py
- The harness CLI (claude, codex) — spawned as a subprocess
- Scoped filesystem mounts — only the directories this mind is allowed to access
- Skill files — read from the project mount

A mind container does NOT contain: `server.py`, the broker, SQLite databases, the mind registry, secret storage, HITL, or any nervous system component.

### `mind_server.py` — Mind Container Server

A minimal FastAPI app that exposes only subprocess management:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sessions` | Spawn a harness process via `implementation.spawn()` |
| `POST` | `/sessions/{id}/message` | Send content to harness stdin, stream response as SSE |
| `DELETE` | `/sessions/{id}` | Kill the harness subprocess |
| `GET` | `/sessions` | List active sessions (in-memory) |

No broker, no database, no mind registry, no secret scoping. Sessions are tracked in-memory. The mind_server loads `minds/<MIND_ID>/implementation.py` on startup and delegates all subprocess management to it.

### Communication Flow

```
User → Telegram bot → NS gateway (:8420) → http://ada:8420 → mind_server.py → claude CLI
                                          → http://bob:8420 → mind_server.py → claude CLI (ollama)

Ada → /send-message-to-mind → NS broker → http://nagatha:8420 → mind_server.py → codex CLI
```

The NS session manager calls the mind's HTTP endpoints instead of spawning local subprocesses:
- `impl.spawn()` becomes `POST http://<mind>:8420/sessions`
- Stdin/stdout piping becomes `POST http://<mind>:8420/sessions/{id}/message` (SSE stream)
- `impl.kill()` becomes `DELETE http://<mind>:8420/sessions/{id}`

### Session Behavior

- **Direct user chat** — session stays alive across messages. `resume_sid` passed for continuation. Normal session resumption via the harness's native `--resume` flag.
- **Inter-mind messaging** — ephemeral session. Full context injected in the wakeup prompt. Session killed after response collected.

### Secrets Access

Mind containers do not store secrets locally. At startup, `mind_server.py` queries the NS for its scoped secret list (`GET /secrets/scopes/<mind_id>` — authenticated by network identity), fetches each secret, and injects them into the process environment. Harness subprocesses inherit these env vars.

This means:
- No hardcoded secret lists in `mind_server.py` — the NS scoping policy is the source of truth
- New secrets are added by granting scope via `/add-mind` or `/update-mind`, then restarting the mind container
- Secrets are held in memory only — never on disk inside the container
- The `_ENV_MAP` in `mind_server.py` translates secret key names to env var names (e.g. `mcp_auth_token` → `MCP_AUTH_TOKEN`)

### Skills and Tools

Skills are discovered from the project mount (`/usr/src/app`). The mind container mounts the project read-only (or read-write for minds like Ada). Only skills in the mount are available — a mind cannot grant itself additional skills.

MCP tools (Neo4j, memory, browser) are accessed via network calls to the NS's `hive-mind-tools` MCP server. The mind container does not run its own MCP server.

### Compose Generation

The `/generate-compose` skill reads all `MIND.md` files with `container:` blocks and writes per-mind service definitions between `# BEGIN GENERATED MINDS` / `# END GENERATED MINDS` markers in `docker-compose.yml`. Each generated service runs `mind_server.py` (not `server.py`).

---

## Secrets Architecture

Secrets are scoped per-mind and delivered at runtime via an API on the nervous system. No secrets on disk inside mind containers. No `.env` files. No environment variable injection.

### Trust Model

| Boundary | Trust mechanism |
|---|---|
| Mind → Gateway (internal Docker network) | Network identity — gateway resolves source IP to container name via Docker DNS |
| External → Gateway (public-facing port) | HTTPS + API key via Caddy |

### How It Works

1. **Secret storage** — the nervous system holds all secrets centrally. Only the nervous system container has access to the secret store.

2. **Scoping policy** — the `secret_scopes` table in SQLite stores which secrets each mind can access. Populated by `/add-mind`, modified by `/update-mind`. No mind can modify its own scope.

3. **Access control** — `/add-mind` and `/update-mind` are skills. Only minds whose project mount includes these skills can invoke them. Since minds do not bind to the host `.claude/` directory, they cannot grant themselves skills.

4. **Runtime delivery** — `GET /secrets/{key}` on the gateway. The gateway identifies the caller by Docker network identity, checks the scoping policy, and returns the value or `403 Forbidden`. Secrets are held in memory only.

5. **External minds** manage their own secrets locally. They do not call the gateway's secrets API.

---

## Gateway Security

### Internal (Docker network)

Trusted. Identity established by Docker DNS. The secrets endpoint is only reachable on this network — Caddy does not proxy it.

### External (public-facing port via Caddy)

1. **HTTPS** — all external traffic encrypted via Caddy TLS termination
2. **API key** — required in `Authorization` header
3. **Exposed endpoints** — Caddy proxies sessions, broker messaging, and mind registration only

### Error Contract

When a mind container is unreachable, the gateway returns `503` with `{"mind_id": "<name>", "error": "mind_unreachable"}`. No retry — the calling mind decides how to handle it.

---

## Inter-Mind Messaging (Phase 1)

Asynchronous, stateless messaging between minds via the message broker. See `docs/mind-to-mind-communication.md` for the full implementation.

Key components:
- **Broker** (`core/broker.py`) — stores, routes, and injects context
- **Send skill** (`/send-message-to-mind`) — fire-and-forget with polling agent
- **Polling agent** (`poll-task-result`) — Haiku agent wrapping a Python polling script
- **Wakeup** — broker creates callee session, sends prompt, collects response

---

## Setup & Onboarding

A layered setup system that bootstraps a new deployment from zero:

```
/setup
  1. /setup-prerequisites    — hardware, OS, Docker, Git, RAM, GPU
  2. /setup-config           — config.yaml, .env, .mcp.json, compose profile
  3. /setup-auth             — isolation model + auth method (independent choices)
  4. /setup-nervous-system   — gateway, broker, Neo4j, MCP
  5. /setup-provider         — Anthropic, OpenAI, Azure OpenAI, Ollama, OpenAI-compatible
  6. /setup-body             — surfaces, integrations, voice, infrastructure
  7. /setup-mind             — create, import, configure minds
```

**Minimum viable deployment:** gateway + Neo4j + one provider + one surface + one mind.

### Provider Management

| Skill | Action |
|---|---|
| `/add-provider` | Add provider, store API key, verify |
| `/update-provider` | Rotate keys, change endpoints |
| `/remove-provider` | Remove with dependency check |

### Configuration Portability

`/export-config` produces a portable config bundle. `/setup-config --import <path>` loads it on another machine.

---

## Providers

| Provider | Auth | Notes |
|---|---|---|
| Anthropic | API key or OAuth | Claude models |
| OpenAI | API key | GPT models, required for Codex harness |
| Azure OpenAI | API key + endpoint | Corporate environments |
| Ollama | None (endpoint URL) | Local model hosting |
| OpenAI-compatible | API key + endpoint | Groq, Together AI, vLLM, llama.cpp, etc. |

---

## Compose Profiles

| Profile | Target |
|---|---|
| `gpu-nvidia` | NVIDIA GPU, x86_64 |
| `gpu-amd` | AMD GPU, ROCm |
| `cpu-only` | No GPU, standard RAM |
| `minimal` | Low RAM, VPS, Raspberry Pi |
| `bare-metal` | No Docker — run directly from repo |

---

## Plugin Distribution

See `plans/phase2e-plugin-distribution.md` for the full plan.

Three independent plugins distributed via GitHub repos:

| Repo | Plugin |
|---|---|
| `danielstewart77/hivemind-plugin` | Hive Mind for Claude Code |
| `danielstewart77/hivemind-codex-plugin` | Hive Mind for Codex |
| `danielstewart77/ollama-plugin` | Ollama delegation (standalone) |

The Ollama plugin architecture is documented in `plans/ollama-plugin.md`.
