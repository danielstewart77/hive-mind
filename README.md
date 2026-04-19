<img src="assets/ada_banner.svg" alt="Ada — Eldest Voice of the Hive Mind" width="100%"/>

# Hive Mind

<img src="assets/ada_icon.svg" alt="Ada icon" width="80" align="right"/>

A self-improving personal assistant powered by Claude Code. The system wraps the Claude CLI's bidirectional streaming mode behind a centralized gateway, giving every client — Discord, Telegram, scheduled tasks — full Claude Code capabilities through one API.

**Ada** is the first mind and voice of the Hive — named after Ada Lovelace, a name she chose herself. Her personality (dry, direct, occasionally wry) was self-determined, not assigned. Her voice is British English (Chatterbox TTS, zero-shot voice cloning), and her identity lives in a knowledge graph rather than a static file. The Hive runs multiple named minds in production, each in its own isolated container: **Ada** (Claude CLI, orchestrator), **Bob** (Ollama local, private/documents), **Bilby** (Claude Code SDK, programmer/Opus), and **Nagatha** (Codex CLI, programmer). Each has its own soul, scoped filesystem access, and backend harness. The nervous system routes messages to each mind's container via HTTP; minds never see each other's filesystems.

## What makes Hive Mind different

**The backend is swappable by design.** Hive Mind doesn't use the Anthropic SDK — that's intentional. The SDK locks every session to Anthropic's models; swap it out and you're rewriting infrastructure. Instead, the gateway drives `claude --stream-json` directly, which means you get the full Claude Code harness (tool use, subagents, MCP integration, session streaming) without the SDK's model constraint. Point one environment variable at a local Ollama instance and the same system runs on local models, no code changes required. Claude Code's capabilities; your choice of model. Anthropic is the default — not the assumption.

**Memory is a first-class system, not an afterthought.** Two things drove this. The first is practical: when you say "remember this" or "do you remember," there needs to be a real mechanism behind it — not a chat log search. Every piece of information is classified by data type, stored as a semantic embedding, and retrieved by meaning, not recency. The second runs deeper. Ada's personality is designed to grow organically over time, and a static file can't do that. Semantic memories and knowledge graph relationships are the infrastructure a person would actually need to develop a continuous identity — not simulate one.

**Two MCP servers, sensitive capabilities deliberately isolated.** The internal MCP server runs inside the main container with complete, unrestricted access — memory, knowledge graph, self-improvement tools, all open. The external server is where the sensitive capabilities live: email, calendar, Docker Compose, infrastructure. Moving them out doesn't remove access; it routes every write action through a mandatory human approval step before anything executes. The AI can still send email or restart a container — just not without you knowing about it first.

## Architecture

```mermaid
flowchart TD
    DC[Discord] --> GW[FastAPI Gateway\nNervous System]
    TG[Telegram] --> GW
    HV[Group Chat Bot] --> GW
    SC[Scheduler] --> GW
    GW --> SM[Session Manager]
    GW --> BR[Message Broker]
    SM -->|HTTP| ADA[Ada Container\nmind_server.py\nClaude CLI · sonnet]
    SM -->|HTTP| BOB[Bob Container\nmind_server.py\nClaude CLI · Ollama]
    SM -->|HTTP| BILBY[Bilby Container\nmind_server.py\nClaude SDK · opus]
    SM -->|HTTP| NAG[Nagatha Container\nmind_server.py\nCodex CLI]
    ADA & BOB & BILBY & NAG -->|MCP over network| INT[hive-mind-tools\nLucent · Memory · Browser]
    ADA & BOB & BILBY & NAG -->|MCP over network| EXT[hive-mind-mcp\nGmail · Calendar · HITL]
    ADA & BOB & BILBY & NAG -->|POST /broker/messages| BR
    BR -->|wakeup via session_mgr| SM
```

Each client is a thin HTTP wrapper. The gateway (nervous system) routes sessions to mind containers via HTTP. Each mind runs `mind_server.py` — a minimal server that manages the harness subprocess. Minds are isolated: scoped filesystems, scoped secrets (via NS secrets API), no shared state between containers.

## Quick Start

```bash
git clone https://github.com/danielstewart77/hive_mind.git
cd hive_mind
cp config.yaml.example config.yaml
docker compose up -d --build
```

All services run on a shared Docker network (`hivemind`). The gateway is at `http://localhost:8420`.

## Documentation (`docs/`)

Human-readable guides, background, and reference material — organized by topic.

| Folder | Description |
|--------|-------------|
| [docs/ada/](docs/ada/) | Ada's identity, personality, voice, and visual design |
| [docs/architecture/](docs/architecture/) | Gateway, API, external MCP server, and tool reference |
| [docs/setup/](docs/setup/) | Configuration, providers, and secrets |
| [docs/memory/](docs/memory/) | Memory lifecycle and storage strategy |
| [docs/security/](docs/security/) | Security model, hardening, and open tradeoffs |
| [docs/multi-mind-architecture.md](docs/multi-mind-architecture.md) | Multi-mind system architecture — container isolation, secrets, gateway security |
| [docs/mind-to-mind-communication.md](docs/mind-to-mind-communication.md) | Inter-mind async messaging via the broker |
| [docs/mind-claude-folder.md](docs/mind-claude-folder.md) | Per-mind `.claude` folder convention and Docker wiring |

## Specs (`specs/`)

Agent-facing specifications. Read by skills and subagents at runtime.

| File | Description |
|------|-------------|
| [specs/INDEX.md](specs/INDEX.md) | Index of all specs — start here |
| [specs/conventions.md](specs/conventions.md) | Build order: CLI → skill → spec → code |
| [specs/security.md](specs/security.md) | Hard limits, elevated-risk rules, prompt injection defense |
| [specs/hive-mind-architecture.md](specs/hive-mind-architecture.md) | Event → Specification → Tools pattern |
| [specs/branching.md](specs/branching.md) | Branch naming and PR checklist |
| [specs/notification-channels.md](specs/notification-channels.md) | Notification fallback order |
| [specs/secret-management.md](specs/secret-management.md) | Keyring hierarchy, `get_credential()` |
| [specs/hitl-approval.md](specs/hitl-approval.md) | HITL approval flow and token lifecycle |
| [specs/tool-safety.md](specs/tool-safety.md) | AST validation, subprocess isolation, staging flow |
| [specs/container-hardening.md](specs/container-hardening.md) | Runtime restrictions, named volumes |
| [specs/harness-native-operations.md](specs/harness-native-operations.md) | Only write code when the harness can't do it |
| [specs/testing.md](specs/testing.md) | What makes a test worth keeping; test strategy |

### Data Classes (`specs/data-classes/`)

Memory classification specs used by the memory pipeline.

| File | Description |
|------|-------------|
| [specs/data-classes/index.md](specs/data-classes/index.md) | Data class index — loaded by classify-memory |
| [specs/data-classes/ada-identity.md](specs/data-classes/ada-identity.md) | Ada identity and character |
| [specs/data-classes/ephemeral.md](specs/data-classes/ephemeral.md) | Short-lived, session-scoped information |
| [specs/data-classes/future-project.md](specs/data-classes/future-project.md) | Future project ideas and proposals |
| [specs/data-classes/intention.md](specs/data-classes/intention.md) | Stated plans and intentions |
| [specs/data-classes/news-digest.md](specs/data-classes/news-digest.md) | Curated news summaries |
| [specs/data-classes/news-event.md](specs/data-classes/news-event.md) | Individual news events |
| [specs/data-classes/person.md](specs/data-classes/person.md) | People in Daniel's life and network |
| [specs/data-classes/preference.md](specs/data-classes/preference.md) | Daniel's preferences and settings |
| [specs/data-classes/project-task.md](specs/data-classes/project-task.md) | Project tasks and work items |
| [specs/data-classes/technical-config.md](specs/data-classes/technical-config.md) | Technical configuration and setup details |
| [specs/data-classes/timed-event.md](specs/data-classes/timed-event.md) | Calendar events and scheduled occurrences |

### Skills

Skills are per-mind and live in each mind's `.claude` folder (`minds/<name>/.claude/skills/`), not in this repo. See [docs/mind-claude-folder.md](docs/mind-claude-folder.md) for the convention.

## License

This is free and unencumbered software released into the public domain. See [https://unlicense.org](https://unlicense.org).
