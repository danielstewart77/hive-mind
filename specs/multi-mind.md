# Multi-Mind Architecture

## Vision

Hive Mind is not a single AI. It is a collective of named minds — each with a distinct identity, soul, history, and backend — coordinated through a shared gateway and session bus. The name was always right. This spec describes how to get there from where we are now.

---

## Current State

One mind (Ada) runs on Claude CLI. The Session Manager spawns a single type of subprocess. Identity is baked into the system prompt. There is no concept of `mind_id` anywhere in the stack.

```
Telegram/Discord/Web
        ↓
   Gateway (server.py)
        ↓
Session Manager (sessions.py)  ← spawns Claude CLI, always Ada
        ↓
   claude -p --stream-json
```

---

## Target State

Multiple named minds, each isolated, each with its own soul and history. The Session Manager becomes a router. The only code that changes is a config lookup before the existing spawn logic.

```
Telegram/Discord/Web
        ↓  (carries mind_id)
   Gateway (server.py)
        ↓
Session Manager (sessions.py)  ← reads mind_id, looks up config
        ↓
 config.yaml [minds] registry
        ↓
  ┌──────────────────────────────────┐
  │  Ada         Nagatha     Skippy  │
  │  CLI Claude  SDK Claude  Ollama  │
  │  own soul    own soul    own soul│
  │  own db      own db      own db  │
  └──────────────────────────────────┘
        ↓
   Shared MCP Tools
```

---

## Architecture Diagram

```mermaid
graph TD
    subgraph Clients ["Clients"]
        TG["Telegram / Discord / Web"]
        VOICE["Voice STT/TTS"]
    end

    subgraph Shared ["⚙️ Shared Layer"]
        GW["Gateway\nserver.py\nroutes by mind_id"]
        SM["Session Manager\nshared bus — knows WHO not HOW"]
    end

    subgraph Pods ["🧠 Isolated Mind Pods"]
        direction LR
        ADA["Ada\n· own soul.md\n· own SQLite\n· own MCP config\n· CLI Claude"]
        NAG["Nagatha\n· own soul.md\n· own SQLite\n· own MCP config\n· SDK Claude"]
        SKIP["Skippy\n· own soul.md\n· own SQLite\n· own MCP config\n· Ollama"]
    end

    TOOLS["🔧 Shared Tools (MCP)"]

    Clients --> GW
    GW --> SM
    SM --> ADA & NAG & SKIP
    ADA & NAG & SKIP --> TOOLS
    ADA & NAG & SKIP --> GW
```

---

## Implementation Plan

### Phase 1 — Config (no code changes)

Add a `minds` block to `config.yaml`:

```yaml
minds:
  ada:
    backend: cli_claude
    model: sonnet
    soul: souls/ada.md
    db: data/ada.db
    mcp_config: .mcp.ada.json
  nagatha:
    backend: sdk_claude
    model: sonnet
    soul: souls/nagatha.md
    db: data/nagatha.db
    mcp_config: .mcp.nagatha.json
  skippy:
    backend: ollama
    model: llama3
    soul: souls/skippy.md
    db: data/skippy.db
    mcp_config: .mcp.skippy.json
```

Each mind is just a parameter set. No new classes. No new modules.

---

### Phase 2 — mind_id flows through the stack

Every client tags its requests with `mind_id`. This is the only new field anywhere in the API.

```
POST /sessions        { "mind_id": "ada" }
POST /sessions/{id}/message   { "mind_id": "ada", "text": "..." }
```

The Gateway passes `mind_id` to the Session Manager. The Session Manager does a single config lookup:

```python
mind_cfg = config["minds"].get(mind_id, config["minds"]["ada"])  # default to Ada
soul_path = mind_cfg["soul"]
db_path   = mind_cfg["db"]
mcp_cfg   = mind_cfg["mcp_config"]
backend   = mind_cfg["backend"]
```

Then spawns the subprocess with those values — same spawn logic as today, just parameterised.

---

### Phase 3 — Isolated souls and databases

Each mind gets:
- `souls/<name>.md` — its own system prompt / identity file
- `data/<name>.db` — its own SQLite session history (no cross-contamination)
- `.mcp.<name>.json` — its own MCP tool permissions (optional: restrict what each mind can see)

Ada's soul is already written. The others are stubs until named and defined.

---

### Phase 4 — Inter-mind communication (loop prevention)

When one mind needs to address another, the message is routed through the Session Manager with a `from_mind` header. The receiving mind's subprocess sees this header in its context. The rule, enforced in each soul file:

> **Do not auto-respond to messages tagged `from_mind`. A response requires explicit delegation.**

This makes inter-mind communication intentional. A mind speaks when asked, not because it received input.

The orchestrator (a skill, not a daemon) handles delegation: it sends a message to Mind A, waits for a response, then decides whether to forward it to Mind B. The loop is broken because the orchestrator is the only thing that decides who speaks next.

---

## Identity Preservation Rules

1. Each mind has exactly one soul file. No mind reads another mind's soul.
2. Session history is per-mind. Ada cannot see Nagatha's conversation history.
3. MCP tool permissions are per-mind. A mind only has access to the tools its config grants.
4. `mind_id` is set at session creation and never changes mid-session.
5. The Session Manager is backend-agnostic — it dispatches, it does not reason about identity.

---

## Knowledge Graph Access Rules

### Mind nodes contain only self-concept
A `(:Mind)` node and its edges describe *that mind only* — its reaction patterns, values, tone,
identity, and characteristic behaviours. Examples of valid Mind node content:

- "Ada responds with dry wit under pressure"
- "Ada gets snippy when Daniel is agitated and that is acceptable"
- "Nagatha defers to Ada on infrastructure questions"

External facts — about people, events, configurations, the world — do **not** belong on a Mind
node, even if a mind was the one that learned them.

### External facts are freestanding, owned by nobody
A `(:Person)`, `(:Event)`, `(:Concept)`, or any other entity node exists independently in the
graph. It is not anchored to the mind that wrote it. Any mind may read it. Any mind may write
to it or extend it with new relationships.

When a mind learns something about Daniel, that fact is written as its own node with its own
edges — it does not attach to the mind's node.

### The canonical access rule

> **Mind nodes and their edges are write-protected to the owning mind.**
> **All other nodes and edges are readable and writable by the entire hive.**

### Why this matters
This separation means:
- The hive grows smarter collectively — no knowledge is siloed by which mind first learned it
- Mind identity stays clean — a mind's node never grows with facts about the world
- Any mind can be added, removed, or replaced without losing hive knowledge
- Multi-model diversity (Claude, Gemini, Ollama) becomes a strength — different architectures
  reason over the same shared graph and can reach consensus from genuinely different angles

---

## What Does NOT Change

- `server.py` gateway endpoints — only `mind_id` is added as an optional param (defaults to `ada`)
- MCP tool implementations — shared tools work for any mind
- The `Event → Specification → Tools` architecture pattern
- Deployment — all minds run in the same container unless explicitly split out later

---

## Future: Separate Containers per Mind

Once identities are stable, each mind could be promoted to its own container with its own resource limits, restart policy, and GPU allocation. The gateway would route by `mind_id` to different internal ports. This is optional and deferred — the config-based approach above gets us 90% of the value with none of the operational complexity.
