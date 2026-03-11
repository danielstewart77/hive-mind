# Human-in-the-Loop (HITL) Approval

## Purpose

Any mutating, destructive, or high-blast-radius action requires explicit human approval before execution. The confirmation uses an out-of-band channel — the approval signal arrives via Telegram, which is unreachable from within the tool execution environment.

## Flow

```
Tool requests action → Gateway generates one-time token (in-memory only)
                     → Pushes notification to Telegram
                     → Human approves or denies
                     → Gateway validates token, proceeds or cancels
```

The tool subprocess never sees the confirmation token. It cannot forge approval because the token is generated after the request, held in gateway memory only, and the approval signal must arrive via an external channel.

## Token Lifecycle

- Tokens have a per-request TTL (default 180s, clamped to 30s–10min)
- Long-running operations (Docker builds) use 600s TTL
- Resolved tokens stay in memory until TTL expires (so polling clients can read the result)
- Expired tokens are cleaned up every 30 seconds by a background task

## Two Modes

### Blocking (`wait=True`, default)
The gateway holds the HTTP connection open until approved, denied, or timeout. The MCP tool gets back `{"approved": true/false}`.

### Non-blocking (`wait=False`)
The gateway returns a token immediately: `{"token": "abc123", "state": "pending"}`. The MCP tool polls `GET /hitl/status/{token}` until it gets `approved`, `denied`, or `expired`.

## Session Heartbeat

The session manager updates `last_active` on every event yielded during response processing. This prevents the idle reaper from killing sessions during HITL waits, Docker builds, or multi-tool chains.

## Telegram Bot Integration

The Telegram bot's HTTP session uses an unlimited timeout (`aiohttp.ClientTimeout(total=0, sock_read=0)`) so it never drops the SSE stream while waiting for HITL approval + operation completion.

Approval requests are sent as Telegram messages with **inline keyboard buttons** (Approve / Deny). Tapping a button calls back to the gateway's `/hitl/respond` endpoint. The original message updates in-place to show the outcome (Approved / Denied / Expired). Double-taps on an already-resolved request are silently ignored.

## Actions Requiring HITL

- Sending, deleting, or modifying email
- Modifying calendar events
- Docker Compose operations (up, down, restart)
- Posting to social media
- Executing shell commands beyond tool scope

## Implementation Files

- `core/hitl.py` — in-memory token store with asyncio.Event-based waiting
- `server.py` — `/hitl/request`, `/hitl/status/{token}`, `/hitl/respond` endpoints
- `clients/telegram_bot.py` — inline keyboard callback handler (`button_callback`), expired-request cleanup loop
- `specs/hitl-telegram-inline-buttons.md` — full design spec for the inline keyboard implementation
