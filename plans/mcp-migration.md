# MCP Tool Migration Plan

> **Principle:** An MCP tool is justified only when the underlying resource requires persistent state across multiple tool calls within a single session. Everything else should be a skill, a script, or a mind call.

---

## The Line

| Criterion | MCP tool | Skill + script | Mind call |
|---|---|---|---|
| Stateful session (browser, websocket, stream) | ✅ required | ✗ | ✗ |
| Expensive persistent connection (DB pool) | acceptable | ✗ | ✗ |
| One-shot REST/HTTP call | ✗ | ✅ preferred | acceptable |
| Requires reasoning, HITL, or composition | ✗ | acceptable | ✅ preferred |
| External service with OAuth / token management | ✗ | acceptable | ✅ preferred |
| Shell command or file operation | ✗ | ✅ preferred | ✗ |

**Mind call** is the right choice when the operation benefits from reasoning — composing an email, deciding what to approve in HITL, interpreting a Docker status. A mind can do HITL natively by asking Daniel and waiting; no special approval infrastructure needed.

---

## Current Tools — Migration Decision

### `hive-mind-tools` (internal MCP server)

| Tool | Stateful? | Decision | Migration target |
|---|---|---|---|
| `browser_*` | ✅ Yes — Playwright session | **KEEP** | — |
| `memory_store`, `memory_retrieve`, `memory_update`, `memory_delete` | No — Neo4j HTTP | **MIGRATE** | Script + skill; or accept persistent connection justification (frequent use) |
| `graph_query`, `graph_upsert`, `graph_upsert_direct` | No — Neo4j bolt | **MIGRATE** | Script + skill |
| `search_person`, `audit_person_nodes`, `update_person_names` | No | **MIGRATE** | Script + skill |
| `delegate_to_mind`, `forward_to_mind` | No — HTTP to gateway | **MIGRATE** | Direct HTTP in skill; already just an API call |
| `web_search` | No | **MIGRATE** | Script + skill |

**Note on Neo4j tools:** The bolt connection pool is a legitimate argument for keeping these as MCP. The counter-argument is that the Neo4j HTTP API works fine for one-shot queries from a script. Revisit after measuring actual latency cost. For now, treat as **low priority migration** — the browser tools are the real reason `hive-mind-tools` exists.

**Immediate action:** Slim `hive-mind-tools` to browser only. Move everything else to scripts or skills. This makes the MCP server's purpose unambiguous.

### `hive-mind-mcp` (external MCP server)

| Tool | Stateful? | Decision | Migration target |
|---|---|---|---|
| `send_email`, `reply_to_email`, `get_email`, `read_emails` | No | **MIGRATE** | Body mind (email mind) or script |
| `create_calendar_event`, `list_calendar_events`, etc. | No | **MIGRATE** | Body mind or script |
| `post_to_linkedin` | No | **MIGRATE** | Script + skill |
| `hitl/*` approval flow | No — HTTP polling | **MIGRATE** | Body mind handles HITL natively |
| `docker_list_containers`, `compose_*` | No — shell commands | **MIGRATE** | Script + skill; or Ada directly via shell |
| Gmail `authenticate` | OAuth token | **MIGRATE** | Token stored in keyring; script uses it |

**`hive-mind-mcp` target state: deleted.** All functionality moves to scripts, skills, or body minds.

---

## Migration Paths

### Path 1 — Script + skill (preferred for simple operations)

Replace the MCP tool call with a Python script in `tools/stateless/<name>/` and a skill that invokes it. Pattern already established by `weather.py`, `crypto.py`, `planka.py`.

**Example:** `docker_list_containers` → `tools/stateless/docker/docker.py` that shells out to `docker ps` and returns JSON. Skill invokes it.

### Path 2 — Mind call (preferred when reasoning or HITL is involved)

Delegate to a body mind via the broker. The mind has:
- Full access to the relevant credentials (keyring)
- Ability to reason about what to do (compose a good email, interpret a Docker error)
- Ability to do HITL natively — ask Daniel over Telegram, wait for reply, act

**Example:** Instead of `send_email` MCP tool, send a broker message to the `body` mind:
```
"Send an email to X about Y. Wait for my approval before sending."
```
The body mind composes, presents a draft to Daniel via Telegram, gets approval, sends. No HITL infrastructure needed — the mind IS the HITL.

**Example:** Instead of `compose_restart` MCP tool, ask Ada directly:
```
/restart-service hive_mind
```
Ada shells out. She already has access. No MCP tool needed.

### Path 3 — Direct shell (Ada only)

For infrastructure operations (Docker, compose, systemctl), Ada can shell out directly when she has the necessary mount. No script wrapper needed. Reserve for Ada's scope only — other minds don't get shell access to the host.

---

## Body Mind Concept

The migration of `hive-mind-mcp` naturally leads to a `body` mind — a lightweight mind whose scope is external service interfaces: email, calendar, social, notifications.

**Profile:**
- Harness: `cli_claude` (needs full Claude reasoning for composition and HITL)
- Model: `haiku` (cost-efficient; most tasks are simple send/retrieve)
- Volumes: none (no filesystem access needed — everything via API/keyring)
- Role: receives broker messages, executes external service calls, handles HITL natively

**The key insight:** HITL via a mind is strictly better than HITL via an approval token flow. The mind can:
- Summarise what it's about to do in natural language
- Accept "yes", "no", "change X to Y" — not just binary approval
- Retry with modifications rather than failing on rejection

---

## Migration Priority

| Priority | Work |
|---|---|
| High | Slim `hive-mind-tools` to browser only — move/delete all non-browser tools |
| High | Delete `hive-mind-mcp` — replace with scripts for simple ops, body mind for reasoning ops |
| Medium | Create `body` mind with email, calendar, notification scope |
| Low | Revisit Neo4j MCP tools after measuring bolt vs HTTP latency |

---

## What This Means for the Architecture

After migration, the Body layer looks like:

| Component | Type |
|---|---|
| Discord Bot | thin client |
| Telegram Bot | thin client |
| Scheduler | thin client |
| Body mind | mind container — external service scope |

And the Nervous System's MCP surface shrinks to a single server: browser automation. Everything else is a script, a skill, or a mind.
