# Human-in-the-Loop (HITL) Approval

## Purpose

Any mutating, destructive, or high-blast-radius action requires explicit human approval before execution. The confirmation uses an out-of-band channel — the approval signal arrives via Telegram, which is unreachable from within the tool execution environment.

## Where it lives

HITL is entirely owned by the external `hive-tools` service, not this repo. `hive-tools` gates a tool call, persists the pending request (DB-backed, not in-memory — survives a restart), and sends its own Telegram notification with inline Approve/Deny buttons directly (it holds its own `TELEGRAM_BOT_TOKEN`). This repo's only role is the callback side: `bots/telegram_bot.py::handle_hitl_callback` receives the button tap and forwards it.

## Flow

```
A mind calls a HITL-gated hive-tools endpoint
  → hive-tools persists the request, returns 202-style "pending"
  → hive-tools sends its own Telegram message: inline Approve/Deny buttons
  → Daniel taps a button
  → Telegram delivers the callback to this repo's bot
  → bots/telegram_bot.py::handle_hitl_callback POSTs
    {hive_tools_url}/hitl/{token}/respond
  → hive-tools resolves the request and the original tool call proceeds or is cancelled
```

## Polling

A caller (or this repo's own retry logic) can also check status directly:
`GET {hive_tools_url}/hitl/{request_id}` on hive-tools.

## Actions Requiring HITL

- Sending, deleting, or modifying email
- Modifying calendar events
- Docker Compose operations (up, down, restart)
- Posting to social media
- Executing shell commands beyond tool scope

## Implementation Files

- `bots/telegram_bot.py::handle_hitl_callback` — inline keyboard callback handler, POSTs to hive-tools' respond endpoint
- hive-tools' own `hitl.py` and `server.py` (`GET /hitl/{id}`, `POST /hitl/{id}/respond`) — request persistence, expiry, and the outbound Telegram notification; see [docs/architecture/hive-tools.md](../docs/architecture/hive-tools.md)

There is no gateway-side HITL endpoint — `hive-comms` has no `/hitl/*` routes at all.
