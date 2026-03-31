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

## MCP Authentication

The external MCP server is protected by a bearer token:
- Token stored in keyring as `MCP_AUTH_TOKEN`
- Bridged into env at gateway startup
- Referenced in `.mcp.container.json` as `${MCP_AUTH_TOKEN}`

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
