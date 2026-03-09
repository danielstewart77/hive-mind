# Notification Channels

The system uses layered notification with automatic fallback. Always prefer the highest-priority available channel.

## Fallback Order

| Priority | Channel | When to use |
|----------|---------|-------------|
| 1 | **Telegram bot** (`notify_owner` MCP tool) | Normal operation — bot is up and gateway is running |
| 2 | **Direct Telegram API** (`notify_owner` MCP tool fallback) | Gateway down, bot still has API access |
| 3 | **Gmail** (via MCP email tool) | Telegram unreachable |
| 4 | **Alert file** (`/usr/src/app/data/alerts.log`) | Last resort — always works, no network required |

The `notify_owner` tool in `agents/notify.py` implements channels 1–4 automatically.

## When to Call notify_owner

**Automated pipeline agents only** — orchestrator, step-coding, 3am, scheduled tasks.

**Never call notify_owner in a live interactive session.** Daniel is already present in the conversation. Sending a Telegram notification while he's talking to you is redundant and noisy.

Appropriate callers:
- Orchestrator (card success, step failure, session end summary)
- 3am nightly session (task completion, errors)
- Scheduled jobs (task failures, health alerts)

Do not implement your own notification logic. Call `notify_owner` and let it handle fallback.
