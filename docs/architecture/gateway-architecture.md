# Gateway Architecture

## Overview

The gateway server (`server.py`) is the single point of entry for all clients. It wraps the Claude CLI's bidirectional stream-json mode, giving every surface (Telegram, Discord, terminal, web) full CLI capabilities through one API.

## Session Manager

`core/sessions.py` manages a pool of Claude CLI subprocesses.

- Each session is a separate `claude` process communicating via stdin/stdout NDJSON
- Sessions are stored in SQLite (`data/sessions.db`)
- Idle sessions are reaped after `idle_timeout_minutes` (default 30)
- `last_active` is updated on every event yielded, preventing reaping during active work
- Max concurrent sessions configurable via `max_sessions` (default 10)

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

`core/models.py` supports multiple providers:
- **Anthropic** (default) — static aliases: sonnet, opus, haiku
- **Ollama** — auto-discovered local models
- Per-subprocess env isolation — no global env mutation

## Message Broker

`core/broker.py` provides asynchronous inter-mind messaging integrated directly into `server.py`. No separate container — it runs in the same process as the gateway and shares the session manager.

**How it works:**
- A mind POSTs to `POST /broker/messages` with `from`, `to`, `content`, and optional `rolling_summary`
- The broker writes the message to `data/broker.db` (SQLite, separate from `sessions.db`) and returns immediately: `{ "status": "dispatched", "conversation_id": "...", "message_id": "..." }`
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
| POST | /hitl/request | HITL approval request |
| GET | /hitl/status/{token} | Poll HITL status |
| POST | /hitl/respond | Resolve HITL (Telegram bot) |
| POST | /sessions/{id}/remote-control | Start remote observation of a session |
| DELETE | /sessions/{id}/remote-control | Stop remote observation |
| POST | /group-sessions | Create group session (multi-mind) |
| GET | /group-sessions/{id} | Get group session detail |
| POST | /group-sessions/{id}/message | Send message to group session |
| DELETE | /group-sessions/{id} | Kill group session |
| POST | /memory/expiry-sweep | Trigger timed-event expiry sweep |
| POST | /epilogue/sweep | Trigger session epilogue sweep |
