# Gateway Architecture

## Overview

The gateway (`hive-comms`, built from `nervous-system/comms/server.py`) is the single point of entry for all clients. It wraps the Claude CLI's (and Codex CLI's) bidirectional stream-json mode, giving every surface (Telegram, Discord, terminal, web) full CLI capabilities through one API.

## Session Manager

`nervous-system/comms/sessions.py` manages a pool of Claude/Codex CLI subprocesses.

- Each session is a separate subprocess communicating via stdin/stdout NDJSON
- Sessions are stored in SQLite (`nervous-system/data/sessions.db`)
- Idle sessions are suspended (not killed) after seven days untouched
  (`REAP_IDLE_AFTER_SECONDS`); one holding a live subprocess is skipped regardless
- `last_active` is updated on every event yielded, preventing reaping during active work

## Streaming

Messages flow as Server-Sent Events (SSE):
- `POST /sessions/{id}/message` returns `text/event-stream`
- Events: `assistant` (text chunks), `tool_use`, `tool_result`, `result` (final)
- WebSocket alternative: `WS /sessions/{id}/stream`

## Client Architecture

Clients are thin — they handle surface-specific I/O and delegate all intelligence to the gateway.

- `core/gateway_client.py` — shared HTTP client used by all bots
- `GatewayClient.query_stream()` — yields text chunks from SSE, unlimited timeout
- `GatewayClient.query()` — non-streaming convenience wrapper

## Model Registry

`nervous-system/comms/models.py` supports multiple providers:
- **Anthropic** (default) — static aliases: sonnet, opus, haiku
- **Ollama** — auto-discovered local models
- Per-subprocess env isolation — no global env mutation

## Message Broker

`nervous-system/comms/broker.py` provides asynchronous inter-mind messaging integrated directly into `comms/server.py`. No separate container — it runs in the same process as the gateway and shares the session manager.

**How it works:**
- A mind POSTs to `POST /broker/messages` with `from`, `to`, `content`, and optional `rolling_summary`
- The broker writes the message to `nervous-system/data/broker.db` (SQLite, separate from `sessions.db`) and returns immediately: `{ "status": "dispatched", "conversation_id": "...", "message_id": "..." }`
- An `asyncio` background task wakes the callee: creates a session via `session_mgr`, sends a wakeup prompt, collects the full SSE response, and writes it back as a new message row
- The caller's polling agent (`tools/stateless/poll_broker/poll_broker.py`) polls `GET /broker/messages?conversation_id=<id>` every 30 seconds until the callee's response appears
- Callee minds never know about the broker — they just respond normally through their session

**Startup recovery:** On gateway start, messages stranded in `dispatched` status (session died on restart) are marked `failed`. Messages in `pending` are returned for re-dispatch.

**Broker endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| POST | /broker/messages | Send message, write to DB, kick off background wakeup |
| GET | /broker/messages | Query messages by `conversation_id` |
| GET | /broker/conversations/{id} | Get conversation with all messages |

## Key API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | /sessions | Create session |
| GET | /sessions | List sessions |
| POST | /sessions/{id}/message | Send message (SSE) |
| POST | /command | Route slash commands |
| POST | /sessions/{id}/remote-control | Start remote observation of a session |
| DELETE | /sessions/{id}/remote-control | Stop remote observation |

HITL approval is not a gateway endpoint — see [gateway-api.md](gateway-api.md#endpoints).
Group-chat session management (`create_group_session` / `get_group_session` /
`delete_group_session`) exists on the session manager but isn't exposed over
HTTP yet — `config.yaml`'s `group_chat` block is read, but nothing currently
drives it end-to-end. Memory pruning is a nightly APScheduler cron inside
`hive-lucent`, not an HTTP sweep — see
[nervous-system/README.md](../../nervous-system/README.md#pruning).
