# Human-in-the-Loop (HITL) Approval

Any mutating, destructive, or high-blast-radius action requires explicit human approval before execution. The confirmation uses an **out-of-band channel** — the approval signal arrives via Telegram, which is unreachable from within the tool execution environment.

## Flow

```
Tool calls require_approval(action, summary)
  → Gateway creates a one-time token (held in memory only)
  → Pushes Telegram notification to Daniel
  → Tool polls /hitl/status/{token} every 5s
  → Daniel approves or denies via Telegram
  → Gateway resolves the token
  → Tool proceeds (approved) or returns error (denied/timed out)
```

The tool subprocess never sees the token. It cannot forge approval because:
- The token is generated *after* the request arrives
- It is held in gateway memory only (never sent to the tool)
- The approval signal must arrive via an external channel (Telegram)

## Token Lifecycle

- **TTL**: default 180s, configurable per action (clamped to 30s–10min)
- **Long-running operations** (e.g. Docker builds): use 600s TTL
- **States**: `pending` → `approved` | `denied` | `expired`
- Tokens are single-use and removed from memory on resolution

## Session Keepalive

The session manager updates `last_active` on every event yielded during response processing. This prevents the idle reaper from killing sessions during HITL waits or long operations.

The Telegram bot's HTTP session uses an unlimited read timeout so it never drops the SSE stream while waiting for approval + operation completion.

## Actions Requiring HITL

| Category | Examples |
|---|---|
| Email | Send, delete, move |
| Calendar | Create, update, delete events |
| Social media | Post to LinkedIn |
| Infrastructure | Docker Compose up/restart/down |
| Code execution | Any shell command beyond tool scope |

Read-only operations (list, read, search, status) never require HITL.

## Implementation

The HITL system lives in two places:

- **Gateway** (`core/hitl.py`, `server.py`) — token store, `/hitl/request` and `/hitl/status/{token}` endpoints, Telegram notification dispatch
- **MCP tools** (`tools/approval.py` in `hive_mind_mcp`) — `require_approval()` helper that creates the request and polls for resolution using non-blocking async calls to keep the MCP SSE connection alive

```python
from tools.approval import require_approval

async def my_write_tool(param: str) -> str:
    denied = await require_approval("action_name", f"Doing: {param}")
    if denied:
        return denied  # JSON error — action was blocked
    # ... proceed
```
