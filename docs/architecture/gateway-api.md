# Gateway API

The FastAPI gateway (`hive-comms`, built from `nervous-system/comms/server.py`, host port 8426) is the single entry point for all clients. Discord, Telegram, the scheduler, and any REST consumer all talk to it — never directly to Claude.

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

HITL approval is not a gateway endpoint — it's handled by `hive-tools`'
own server (`GET /hitl/{request_id}`, `POST /hitl/{request_id}/respond`),
called directly by the bot that needs approval (see
[hive-tools.md](hive-tools.md)).

## Creating a Session

```http
POST /sessions
Content-Type: application/json

{
  "owner_type": "terminal",
  "owner_ref": "alice",
  "client_ref": "terminal-1",
  "model": "sonnet",
  "surface_prompt": "Optional context prepended to the session",
  "allowed_directories": ["<external-project-path>"]
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
| `/model [name]` | Switch model on the active session, or list available models with no argument |
| `/autopilot` | Toggle autopilot (no approval prompts) |
| `/sessions` | List selectable sessions |
| `/switch <id\|number>` | Activate a different session on this surface |
| `/kill <id\|number>` | Kill a specific session |
| `/prune` | Kill every session for this owner except the active one |
| `/status` | Session counts (total / running) |
| `/remember` | Points at the automatic memory pipeline — no direct action |

## Directory Access

Claude Code sessions use a two-layer model to access directories outside `/usr/src/app`:

**Layer 1 — Bind mount** (the mind's own `container/compose.yaml` fragment): the host path must be mounted into the mind's container at the same path on both sides, so `--allowedDirectory` values match on the host and inside the container. There's no fixed set of mount env vars — add whatever bind mount your use case needs to the fragment (see `minds/example/container/compose.yaml` for the pattern: `${HOST_PROJECT_DIR:-.}:/usr/src/app:rw` is the one every mind already gets).

**Layer 2 — Per-session permission** (`--allowedDirectory`): Bind mounts alone do not grant Claude Code access. Each session must explicitly request permission at creation time via `allowed_directories`, or via the `/new` command:

```
/new <external-project-path>
```

Both layers are required. Neither works without the other.

## Session Model

Each session is a Claude CLI (or Codex CLI) subprocess managed by `nervous-system/comms/sessions.py`. Sessions are stored in SQLite (`nervous-system/data/sessions.db`). The session manager handles:

- **Process pool**: one subprocess per active session
- **Idle reaper**: kills sessions idle for longer than `idle_timeout_minutes` (default 30)
- **Last-active tracking**: updated on every streamed event, so HITL waits don't trigger the reaper
- **Resume**: sessions can be resumed by passing `resume_session_id` at creation
