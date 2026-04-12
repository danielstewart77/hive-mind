# Gateway API

The FastAPI gateway (`server.py`, port 8420) is the single entry point for all clients. Discord, Telegram, the scheduler, and any REST consumer all talk to it — never directly to Claude.

## Endpoints

### Sessions

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/sessions` | Create a new session |
| `GET` | `/sessions` | List active sessions |
| `GET` | `/sessions/{id}` | Get session detail |
| `DELETE` | `/sessions/{id}` | Kill a session |
| `POST` | `/sessions/{id}/message` | Send a message (SSE streaming response) |
| `POST` | `/sessions/{id}/activate` | Activate session on a surface |
| `POST` | `/sessions/{id}/model` | Switch model mid-session |
| `POST` | `/sessions/{id}/autopilot` | Toggle autopilot mode |
| `WS` | `/sessions/{id}/stream` | WebSocket bidirectional stream |

### Other

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/models` | List available models |
| `POST` | `/command` | Route slash commands (`/new`, `/clear`, `/model`, etc.) |
| `GET` | `/linkedin/auth` | Initiate LinkedIn OAuth flow |
| `GET` | `/linkedin/callback` | LinkedIn OAuth callback (exchanges code, stores token) |
| `POST` | `/hitl/request` | Create a HITL approval request (used by MCP tools) |
| `GET` | `/hitl/status/{token}` | Poll HITL approval status |

## Creating a Session

```http
POST /sessions
Content-Type: application/json

{
  "owner_type": "terminal",
  "owner_ref": "daniel",
  "client_ref": "terminal-1",
  "model": "sonnet",
  "surface_prompt": "Optional context prepended to the session",
  "allowed_directories": ["<mcp-project-path>"]
}
```

`allowed_directories` grants Claude Code access to paths outside the default working directory (`/usr/src/app`). See [Directory Access](#directory-access) below.

## Sending a Message

```http
POST /sessions/{id}/message
Content-Type: application/json

{
  "content": "Your message here"
}
```

Response is an SSE stream. Each event is a JSON object with `type` and `content` fields. The stream closes when Claude finishes responding.

## Slash Commands

`POST /command` handles slash commands routed from clients:

| Command | Effect |
|---|---|
| `/new [dir...]` | Kill active session, create a new one |
| `/clear [dir...]` | Alias for `/new` |
| `/model <name>` | Switch model on active session |
| `/autopilot` | Toggle autopilot (no approval prompts) |
| `/sessions` | List active sessions |
| `/kill <id\|number>` | Kill a specific session |

## Directory Access

Claude Code sessions use a two-layer model to access directories outside `/usr/src/app`:

**Layer 1 — Bind mount** (`docker-compose.yml`): The host path must be mounted into the container. Currently configured on the `server` service:

| Env var | Default host path | Container path |
|---------|-------------------|----------------|
| `HOST_MCP_DIR` | `<mcp-project-path>` | same |
| `HOST_SPARK_DIR` | `<spark-to-bloom-path>` | same |

Paths are mounted at the same location on both sides so `--allowedDirectory` values match.

**Layer 2 — Per-session permission** (`--allowedDirectory`): Bind mounts alone do not grant Claude Code access. Each session must explicitly request permission at creation time via `allowed_directories`, or via the `/new` command:

```
/new <mcp-project-path>
```

Both layers are required. Neither works without the other.

## Session Model

Each session is a Claude CLI subprocess (`claude -p --stream-json`) managed by `core/sessions.py`. Sessions are stored in SQLite (`data/sessions.db`). The session manager handles:

- **Process pool**: one subprocess per active session
- **Idle reaper**: kills sessions idle for longer than `idle_timeout_minutes` (default 30)
- **Last-active tracking**: updated on every streamed event, so HITL waits don't trigger the reaper
- **Resume**: sessions can be resumed by passing `resume_session_id` at creation
