# Centralized CLI Gateway — Server Plan

## Context

The Hive Mind project currently has two ways to invoke Claude Code:
1. **Python SDK** (`claude-agent-sdk`) in `discord_bot.py` — limited (no skills, subagents, hooks, memory)
2. **CLI subprocess** in `agents/skill_*.py` — full power but fire-and-forget, no session continuity

We need a **single gateway server** that wraps the Claude CLI's bidirectional stream-json mode, giving every client (Discord, terminal, web) full CLI capabilities through one API.

### Key Discovery

The CLI supports a persistent bidirectional NDJSON protocol:
```bash
claude -p \
  --input-format stream-json \
  --output-format stream-json \
  --include-partial-messages \
  --permission-mode bypassPermissions \
  --dangerously-skip-permissions
```
- **Input** (stdin): `{"type":"user","message":{"role":"user","content":"..."}}`
- **Output** (stdout): system init → stream events → assistant messages → result
- Multi-turn: after receiving a `result`, send the next `user` message on the same stdin
- Session ID captured from output messages; usable with `--resume` if process dies

---

## Architecture

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│ Discord Bot │  │ Terminal UI │  │   Web UI    │
│  (thin)     │  │  (thin)     │  │  (thin)     │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       └────────────────┼────────────────┘
                        │  HTTP / WebSocket
                 ┌──────▼──────┐
                 │   FastAPI   │
                 │   Gateway   │  ← server.py
                 └──────┬──────┘
                        │
              ┌─────────▼─────────┐
              │  Session Manager  │  ← sessions.py
              │  (process pool +  │
              │   SQLite DB)      │
              └─────────┬─────────┘
                        │  stdin/stdout (NDJSON)
              ┌─────────▼─────────┐
              │  claude -p        │
              │  --input stream   │
              │  --output stream  │
              │  + MCP tools      │
              │  (one per session)│
              └───────────────────┘
```

---

## Files to Create / Modify

```
hive_mind/
├── server.py              # FastAPI gateway (HTTP + WebSocket endpoints)
├── sessions.py            # Session manager (process pool + SQLite)
├── models.py              # Model registry (static aliases + Ollama auto-discovery)
├── discord_bot.py         # Rewrite: thin client calling server.py
├── config.py              # Update: remove backend switching, add provider config
└── config.yaml            # Update: providers block, server settings
```

---

## 1. Session Manager (`sessions.py`)

Core class that owns all Claude CLI subprocesses and the session database.

### SQLite Schema

```sql
-- Every conversation thread, owned by a user
CREATE TABLE sessions (
    id            TEXT PRIMARY KEY,  -- our UUID, used as API key
    claude_sid    TEXT,              -- CLI's session_id (from result messages)
    owner_type    TEXT NOT NULL,     -- "discord" | "terminal" | "web"
    owner_ref     TEXT NOT NULL,     -- discord user id, terminal uid, web user id
    summary       TEXT,              -- auto-generated from first message
    model         TEXT,
    autopilot     INTEGER NOT NULL DEFAULT 0,  -- 0=supervised, 1=autopilot
    created_at    REAL NOT NULL,     -- time.time()
    last_active   REAL NOT NULL,
    status        TEXT NOT NULL      -- "running" | "idle" | "closed"
);

-- Which session is active on a given surface (channel, terminal, browser tab)
-- A user can have many sessions but each surface points to exactly one
CREATE TABLE active_sessions (
    client_type   TEXT NOT NULL,     -- "discord" | "terminal" | "web"
    client_ref    TEXT NOT NULL,     -- discord channel_id, terminal session, browser tab
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    PRIMARY KEY (client_type, client_ref)
);
```

**Why two tables?** A Discord *user* owns sessions (they created them). A Discord *channel* routes to one active session at a time. This lets a user build up a library of sessions and switch any channel between them.

### Process Lifecycle

```
create_session(owner_type, owner_ref, client_ref)
  → spawn claude subprocess (stream-json mode)
  → insert sessions row (status="running")
  → insert active_sessions row (client_ref → new session)
  → auto-generate summary from first message (or set to "New session")
  → return session id

send_message(session_id, content) → AsyncIterator[dict]
  → if process dead/idle: respawn with --resume <claude_sid>
  → write NDJSON line to stdin
  → yield NDJSON lines from stdout until "result" message
  → update last_active, capture claude_sid from result
  → if summary is still "New session": set summary from first user message (truncated)

activate_session(session_id, client_type, client_ref)
  → upsert active_sessions row (client_ref → session_id)
  → if session is idle: respawn with --resume <claude_sid>
  → return session info

list_sessions(owner_ref) → list[dict]
  → query sessions by owner_ref
  → return [{id, summary, status, last_active, model, autopilot}, ...]

toggle_autopilot(session_id) → dict
  → flip autopilot flag in DB
  → kill current process
  → respawn with --resume (with or without --dangerously-skip-permissions)
  → return updated session info

kill_session(session_id)
  → send SIGTERM to subprocess (SIGKILL after 5s grace)
  → update DB row (status="closed")
  → remove from active_sessions where session_id matches
  → return summary of killed session (name, model, autopilot, uptime)

close_session(session_id)
  → alias for kill_session — same behavior

idle_reaper (background task, runs every 60s)
  → find sessions where last_active > idle_timeout
  → terminate subprocess, set status="idle"
  → on next send_message or activate, respawn with --resume

autopilot_guard (background task, runs every 30s)
  → for each running autopilot session:
    → check turns_since_user_input (tracked from result messages)
    → check time_since_last_user_input
    → if either exceeds limit: kill session, set status="killed_guard"
    → log the kill for audit
  (--max-budget-usd is enforced by the CLI itself, not the guard)
```

### Subprocess Spawn

Each process gets its own environment based on the model's provider.
No global env mutation — processes are fully isolated.

**Autopilot mode** controls whether `--dangerously-skip-permissions` is passed.
When off (supervised), the CLI uses `--permission-mode bypassPermissions` which
still allows tool use but respects certain safety checks. When on (autopilot),
all permission checks are skipped entirely.

```python
async def _spawn(self, session_id: str, model: str,
                 autopilot: bool = False,
                 resume_sid: str | None = None) -> Process:
    cmd = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--permission-mode", "bypassPermissions",
        "--model", model,
        "--mcp-config", str(PROJECT_DIR / ".mcp.json"),
        "--append-system-prompt", HIVE_MIND_PROMPT,
    ]
    if autopilot:
        cmd.append("--dangerously-skip-permissions")
        cmd.extend(["--max-budget-usd", str(config.autopilot_max_budget_usd)])
    if resume_sid:
        cmd.extend(["--resume", resume_sid])

    # Per-process env based on provider (no global os.environ mutation)
    provider = model_registry.get_provider(model)
    env = os.environ.copy()
    env.update(provider.env_overrides)

    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(PROJECT_DIR),
    )
```

### Message Protocol

```python
async def send_message(self, session_id: str, content: str) -> AsyncIterator[dict]:
    proc = self._get_or_respawn(session_id)

    # Write user message as NDJSON
    msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content}
    }) + "\n"
    proc.stdin.write(msg.encode())
    await proc.stdin.drain()

    # Read response lines until we get a "result" message
    async for line in proc.stdout:
        event = json.loads(line)
        yield event

        if event.get("type") == "result":
            # Capture CLI session_id for resume capability
            self._update_session(session_id, claude_sid=event.get("session_id"))
            break
```

---

## 2. Model Registry (`models.py`)

Resolves model names to providers and builds the per-process env overrides.
No `model_id` versioning — aliases like `sonnet`, `opus` are passed directly
to `--model` and the CLI resolves to the latest version internally.

### Provider Config (`config.yaml`)

```yaml
providers:
  anthropic: {}   # no env overrides — CLI uses default OAuth creds
  ollama:
    env:
      ANTHROPIC_AUTH_TOKEN: "ollama"
      ANTHROPIC_API_KEY: ""
      ANTHROPIC_BASE_URL: "http://203.0.113.10:11434"
    api_base: "http://203.0.113.10:11434"   # for model discovery

# Static model aliases (Anthropic only — Ollama auto-discovered)
models:
  sonnet: anthropic
  opus: anthropic
  haiku: anthropic
```

### Implementation

```python
@dataclass
class Provider:
    name: str
    env_overrides: dict[str, str]   # applied per-subprocess
    api_base: str | None = None     # for model discovery (Ollama)

class ModelRegistry:
    def __init__(self, providers: dict[str, Provider], static_models: dict[str, str]):
        self._providers = providers
        self._static = static_models           # {"sonnet": "anthropic", ...}
        self._ollama_cache: list[str] = []
        self._ollama_cache_ts: float = 0

    def get_provider(self, model: str) -> Provider:
        """Resolve model name → provider. Static aliases first, then Ollama."""
        if model in self._static:
            return self._providers[self._static[model]]
        # If not a known alias, assume Ollama (it's the dynamic provider)
        if "ollama" in self._providers:
            return self._providers["ollama"]
        raise ValueError(f"Unknown model: {model}")

    async def list_models(self) -> list[dict]:
        """Merge static aliases + live Ollama models into one flat list."""
        result = []
        # Static (Anthropic)
        for alias, provider in self._static.items():
            result.append({"name": alias, "provider": provider})
        # Ollama (auto-discovered, cached 60s)
        if "ollama" in self._providers:
            models = await self._fetch_ollama_models()
            for m in models:
                result.append({"name": m, "provider": "ollama"})
        return result

    async def _fetch_ollama_models(self) -> list[str]:
        """GET /api/tags from Ollama server, return model names."""
        if time.time() - self._ollama_cache_ts < 60:
            return self._ollama_cache
        api_base = self._providers["ollama"].api_base
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{api_base}/api/tags") as resp:
                data = await resp.json()
                self._ollama_cache = [m["name"] for m in data.get("models", [])]
                self._ollama_cache_ts = time.time()
        return self._ollama_cache
```

### `/model` Command Flow

```
User: /model
  → GET /models
  ← Flat list:
      sonnet (anthropic)
      opus (anthropic)
      haiku (anthropic)
      gpt-oss:20b-32k (ollama)
      qwen3:8b (ollama)
      glm-4.7-flash-32k:latest (ollama)

User: /model qwen3:8b
  → POST /sessions/{id}/model  {"model": "qwen3:8b"}
  ← Gateway resolves provider → ollama
  ← Kills current process
  ← Respawns with: --model qwen3:8b + ollama env vars + --resume <claude_sid>
  ← "Switched to qwen3:8b (ollama). Session resumed."

  (If cross-provider switch, adds warning:
   "Context from previous anthropic model may not carry over perfectly.")
```

---

## 3. FastAPI Gateway (`server.py`)

Thin HTTP/WebSocket layer over the session manager.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/sessions` | Create session → returns `{id, summary}` |
| `GET` | `/sessions` | List sessions (filter by `owner_ref`, `status`) |
| `GET` | `/sessions/{id}` | Get session detail |
| `DELETE` | `/sessions/{id}` | Kill session (SIGTERM → SIGKILL after 5s, mark closed) |
| `POST` | `/sessions/{id}/message` | Send message → SSE streaming response |
| `POST` | `/sessions/{id}/activate` | Set as active session for a client surface |
| `POST` | `/sessions/{id}/model` | Switch model mid-session (kill + respawn with --resume) |
| `POST` | `/sessions/{id}/autopilot` | Toggle autopilot on/off (kill + respawn with --resume) |
| `WS` | `/sessions/{id}/stream` | WebSocket bidirectional stream |
| `GET` | `/models` | List all available models (static + Ollama auto-discovered) |

### SSE Endpoint (for simple clients)

```python
@app.post("/sessions/{session_id}/message")
async def send_message(session_id: str, body: MessageRequest):
    async def event_stream():
        async for event in session_mgr.send_message(session_id, body.content):
            yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### WebSocket Endpoint (for interactive clients)

```python
@app.websocket("/sessions/{session_id}/stream")
async def ws_stream(ws: WebSocket, session_id: str):
    await ws.accept()
    while True:
        data = await ws.receive_json()
        async for event in session_mgr.send_message(session_id, data["content"]):
            await ws.send_json(event)
```

### Slash Command Routing

Commands are split between server-level and CLI-passthrough:

**Server handles** (intercepted before reaching Claude):
- `/clear` → close + recreate session
- `/model` → list available models (merged: anthropic aliases + ollama auto-discovered)
- `/model <name>` → switch model mid-session (kill process, respawn with `--resume` + new model/env)
- `/autopilot` → toggle autopilot mode (kill process, respawn with `--resume` ± `--dangerously-skip-permissions`)
- `/kill <id or number>` → kill any session by id or list number (no active session required)
- `/status` → return server + session info (includes autopilot state)
- `/sessions` → list user's sessions with summaries and status badges
- `/switch <id or number>` → activate a different session on this surface
- `/new` → create a new session and activate it

**CLI handles** (passed through as regular messages):
- `/commit`, `/review-pr`, and all Claude Code skills
- These work naturally because the CLI process has full skill support

```python
SERVER_COMMANDS = {"/clear", "/model", "/autopilot", "/kill", "/status", "/sessions", "/switch", "/new"}

async def route_message(session_id: str, content: str, owner_ref: str, client_ref: str):
    cmd = content.split()[0] if content.startswith("/") else None
    if cmd in SERVER_COMMANDS:
        return handle_server_command(cmd, content, owner_ref, client_ref)
    else:
        return session_mgr.send_message(session_id, content)
```

### Session Switching Flow (any client)

```
1. Client sends: /sessions
   → GET /sessions?owner_ref=<user_id>
   ← Returns list: [{id, summary, status, last_active}, ...]
   ← Client formats and displays to user

2. Client sends: /switch 2      (or /switch abc-1234)
   → POST /sessions/abc-1234/activate  {client_type, client_ref}
   ← Session respawned if idle, now active on this surface
   ← Client confirms: "Switched to: <summary>"

3. Client sends: /new
   → POST /sessions  {owner_type, owner_ref, client_ref}
   ← New session created and activated on this surface
   ← Client confirms: "New session started."
```

---

## 4. Discord Bot Rewrite (`discord_bot.py`)

Becomes a thin HTTP client — no SDK dependency, no subprocess management.

### Before (current)
```python
from claude_agent_sdk import query, ClaudeAgentOptions, ...
# 100+ lines of SDK invocation, session tracking, MCP wiring
```

### After
```python
import aiohttp

SERVER_URL = f"http://localhost:{config.server_port}"

async def _get_active_session(user_id: int, channel_id: int) -> str | None:
    """Get the active session for this channel, if any."""
    async with http.get(
        f"{SERVER_URL}/sessions",
        params={"client_type": "discord", "client_ref": str(channel_id)},
    ) as resp:
        data = await resp.json()
        return data[0]["id"] if data else None

async def _ensure_session(user_id: int, channel_id: int) -> str:
    """Get active session for this channel, or create one."""
    session_id = await _get_active_session(user_id, channel_id)
    if session_id:
        return session_id
    async with http.post(f"{SERVER_URL}/sessions", json={
        "owner_type": "discord",
        "owner_ref": str(user_id),
        "client_ref": str(channel_id),
    }) as resp:
        return (await resp.json())["id"]

async def _query(prompt: str, user_id: int, channel_id: int) -> str:
    session_id = await _ensure_session(user_id, channel_id)
    texts = []
    async with http.post(
        f"{SERVER_URL}/sessions/{session_id}/message",
        json={"content": prompt},
    ) as resp:
        async for line in resp.content:
            event = json.loads(line.removeprefix("data: "))
            if event["type"] == "assistant":
                for block in event["message"]["content"]:
                    if block["type"] == "text":
                        texts.append(block["text"])
    return "\n".join(texts)
```

The bot drops its `claude-agent-sdk` dependency entirely. Auth, chunking, and Discord UI logic stay.

### Discord Session Switching UX

```
User: /sessions
Bot:
  📋 Your Sessions:
  1. 🟢 abc-1234 — "Help me refactor the auth module" [sonnet] (2 min ago)
  2. 💤🤖 def-5678 — "Write a Discord bot webhook" [sonnet, autopilot] (3 hours ago)
  3. 💤 ghi-9012 — "Debug the Neo4j connection" [qwen3:8b] (yesterday)

  /switch <number> to resume · /new to start · /kill <number> to kill

User: /switch 2
Bot: ✅ Resumed session "Write a Discord bot webhook" 🤖 autopilot

User: /autopilot
Bot: 🔒 Autopilot OFF for session "Write a Discord bot webhook"
     (Claude will now ask for permission before risky actions)

User: /autopilot
Bot: 🤖 Autopilot ON for session "Write a Discord bot webhook"
     (Claude will execute all actions without asking)

User: /kill 3
Bot: 💀 Killed session "Debug the Neo4j connection"
     (was idle, qwen3:8b, ran for 2h 15m)
```

Status badges in session list:
- 🟢 = running, 💤 = idle, 🔴 = closed/killed
- 🤖 = autopilot enabled (appended when on)

The `/sessions`, `/switch`, `/autopilot`, and `/kill` commands are handled by the
gateway — they don't require an active session and never reach a Claude process.
Regular messages and Claude slash commands (`/commit`, etc.) pass through.

---

## 5. Config Changes

### `config.yaml`

```yaml
# Gateway server
server_port: 8420
idle_timeout_minutes: 30
max_sessions: 10
default_model: sonnet

# Autopilot guard rails — limits that trigger automatic session kill
autopilot_guards:
  max_budget_usd: 5.00            # CLI-enforced: --max-budget-usd (process self-terminates)
  max_turns_without_input: 50     # server-enforced: kill after N agentic turns with no user message
  max_minutes_without_input: 30   # server-enforced: kill after N minutes with no user message

# Providers — env overrides applied per-subprocess, not globally
providers:
  anthropic: {}
  ollama:
    env:
      ANTHROPIC_AUTH_TOKEN: "ollama"
      ANTHROPIC_API_KEY: ""
      ANTHROPIC_BASE_URL: "http://203.0.113.10:11434"
    api_base: "http://203.0.113.10:11434"

# Static model → provider mappings (Ollama models auto-discovered)
models:
  sonnet: anthropic
  opus: anthropic
  haiku: anthropic
```

### `config.py` changes

Remove:
- `backend` field and `switch_backend()` method
- `apply_backend_env()` (no more global env mutation)
- `active_model` property
- `anthropic_model`, `ollama_model`, `ollama_server`, `ollama_port` fields

Add:
- `providers: dict` — loaded from config.yaml
- `models: dict` — static alias mappings
- `default_model: str`
- `server_port: int`, `idle_timeout_minutes: int`, `max_sessions: int`

The `HiveMindConfig` dataclass becomes simpler — provider/model resolution moves to `models.py`.

---

## 6. Dependencies

### Add
```
aiosqlite     # async SQLite for session DB
```

### Remove (from discord_bot.py)
```
claude-agent-sdk   # no longer needed — CLI is the interface
```

Keep `claude-agent-sdk` in requirements.txt for now (other code may use it), but the gateway replaces its role.

---

## 7. Implementation Order

1. **`models.py`** — Model registry
   - Provider dataclass with env_overrides
   - Static alias loading from config.yaml
   - Ollama auto-discovery from `/api/tags` (cached 60s)
   - `get_provider(model)` → Provider
   - `list_models()` → merged flat list

2. **`config.py`** — Simplify config
   - Remove backend/switch_backend/apply_backend_env
   - Add providers, models, server settings from config.yaml
   - No more global env var mutation

3. **`sessions.py`** — Session manager with SQLite + subprocess pool
   - DB init, CRUD operations
   - Process spawn/kill/respawn with per-process env
   - `send_message()` async iterator
   - Model switching: kill + respawn with `--resume` + new model/env
   - Autopilot toggle: kill + respawn with `--resume` ± `--dangerously-skip-permissions`
   - `kill_session()` with SIGTERM → SIGKILL escalation
   - Idle reaper background task
   - Autopilot guard background task (turn limit, time limit, budget via CLI)
   - Unit test: spawn, send, receive, resume after kill

4. **`server.py`** — FastAPI gateway
   - REST endpoints (sessions CRUD, model list, model switch, autopilot toggle)
   - SSE message endpoint
   - WebSocket endpoint
   - Slash command routing (`/model`, `/sessions`, `/switch`, `/new`, `/clear`, `/status`, `/autopilot`, `/kill`)
   - Integration test: full round-trip via HTTP

5. **`discord_bot.py`** — Rewrite as thin client
   - Replace SDK calls with HTTP calls to gateway
   - Keep auth, chunking, Discord UI logic
   - `/model` slash command shows picker, switches via gateway
   - Test: bot responds via gateway

6. **`config.yaml` + `CLAUDE.md`** — Update docs and config structure

---

## 8. Known Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Claude process dies mid-conversation | Respawn with `--resume <claude_sid>` — CLI persists sessions to disk |
| Idle processes consuming resources | Reaper kills after `idle_timeout_minutes`, respawns on demand |
| Concurrent messages to same session | Per-session asyncio.Lock (same pattern as current discord_bot.py) |
| Stream-json duplicate session entries ([#5034](https://github.com/anthropics/claude-code/issues/5034)) | Monitor, deduplicate in DB by claude_sid |
| `bypassPermissions` security | Gateway is localhost-only; auth enforced at client layer (Discord allowlist, API keys for web) |
| Cross-provider model switch loses context | Best-effort: `--resume` replays session history. Warn user on cross-provider switch. Same-provider switches (sonnet→opus, qwen→glm) are seamless |
| Ollama server unreachable | `list_models()` returns stale cache or empty Ollama list; session spawn fails gracefully with clear error |
| Autopilot enables destructive actions | Only allowlisted users can toggle; visible in `/sessions` and `/status`; three guard rails: `--max-budget-usd` (CLI self-terminates), turn limit, time limit. Manual `/kill` always available |
| Runaway autopilot session | Autopilot guard task checks every 30s. Exceeding turn/time/budget limits triggers automatic kill with `status="killed_guard"` for audit. Users can also `/kill` manually from any surface without needing an active session |

---

## 9. Verification

1. **Basic round-trip**: Start `server.py`, create a session via curl, send messages, verify streaming response
   ```bash
   # Create session (defaults to config.default_model)
   curl -X POST localhost:8420/sessions -H 'Content-Type: application/json' \
     -d '{"owner_type":"terminal","owner_ref":"dan","client_ref":"term1"}'

   # Send message (SSE stream)
   curl -N -X POST localhost:8420/sessions/{id}/message \
     -H 'Content-Type: application/json' -d '{"content":"hello"}'
   ```

2. **Resume after idle**: Kill the claude subprocess, send another message, verify it respawns with `--resume` and retains context

3. **Multi-session switching**: Create two sessions for same owner, activate each on same client_ref, verify context switches correctly
   ```bash
   # Create session A, send a message about topic X
   # Create session B, send a message about topic Y
   # Activate session A on same surface
   # Send "what were we talking about?" → should answer topic X
   ```

4. **Session listing**: `GET /sessions?owner_ref=dan` returns all sessions with summaries and status

5. **Model listing**: `GET /models` returns merged list (anthropic aliases + ollama auto-discovered)
   ```bash
   curl localhost:8420/models
   # [{"name":"sonnet","provider":"anthropic"},{"name":"qwen3:8b","provider":"ollama"},...]
   ```

6. **Model switching**: Switch mid-session, verify process respawns with new model and retains context
   ```bash
   # Start session on sonnet, discuss topic X
   curl -X POST localhost:8420/sessions/{id}/model \
     -H 'Content-Type: application/json' -d '{"model":"qwen3:8b"}'
   # Send "what were we talking about?" → should answer topic X (best-effort cross-provider)
   ```

7. **Autopilot toggle**: Toggle autopilot on a running session, verify process respawns with/without `--dangerously-skip-permissions`
   ```bash
   curl -X POST localhost:8420/sessions/{id}/autopilot
   # {"autopilot": true, ...}  — process respawned with --dangerously-skip-permissions + --max-budget-usd
   curl -X POST localhost:8420/sessions/{id}/autopilot
   # {"autopilot": false, ...} — process respawned without those flags
   ```

8. **Kill session**: Kill a running session, verify subprocess terminated and status updated
   ```bash
   curl -X DELETE localhost:8420/sessions/{id}
   # {"status": "closed", "summary": "...", "uptime": "2h 15m"}
   ```

9. **Autopilot guard rails**: Start autopilot session, let it run past `max_turns_without_input`, verify guard kills it automatically
   ```bash
   # Create autopilot session, send one message, wait for guard to trigger
   # GET /sessions/{id} → status should be "killed_guard"
   ```

10. **Discord bot**: Start rewritten bot, test `/sessions`, `/switch`, `/new`, `/model`, `/autopilot`, `/kill`, and regular conversation

11. **Skills passthrough**: Send a message that triggers a Claude skill (e.g., `/commit`), verify the CLI handles it end-to-end

12. **Server commands**: Test `/clear`, `/status`, `/model`, `/autopilot`, `/kill` via Discord, verify gateway-level handling
