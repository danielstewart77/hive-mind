# Scheduled Tasks

Scheduled tasks in Hive Mind are skills that fire on a cron schedule. The schedule lives on the skill itself — there is no separate scheduler config to maintain.

## How it works

The `scheduler` container walks `minds/*/.claude/skills/*/SKILL.md` at startup, picks up any skill whose frontmatter declares a `schedule:` field, and registers a cron job for it. When the cron fires, the scheduler:

1. Creates a fresh session for the owning mind via the gateway.
2. Sends `Run /<skill-name>` to that session.
3. Reads the assistant response from the SSE stream.
4. Deletes the session.
5. Delivers the response via Telegram (text always, voice if `voice: true`).

Each fire is a brand-new session. Sessions are not resumed across fires — cross-fire continuity comes from the mind's persistent memory layer (knowledge graph, vector store), not from chat history.

## Adding a scheduled task

Add (or edit) a skill under `minds/<mind>/.claude/skills/<skill-name>/SKILL.md` and put `schedule:` in the frontmatter:

```yaml
---
name: morning-briefing
description: Daily morning briefing.
schedule: "0 7 * * *"
schedule_timezone: "America/Chicago"
voice: true
---

# Morning Briefing

…body of the skill: what the mind should do when this fires…
```

Save the file. The scheduler reconciles its job list against `minds/*/.claude/skills/` every 30 seconds, so the new task is live within half a minute of writing the file. No restart needed.

The pickup is logged:

```
[INFO] hive-mind-scheduler: Scheduled <mind>/<skill> @ <cron> (<timezone>)
[INFO] hive-mind-scheduler: Reconcile: +1 / -0 skill job(s)
```

Editing an existing skill's `schedule:` (or `schedule_timezone:` / `voice:` / `notify:`) is treated as a remove + re-add — the old job is unscheduled and the new one registered, all within the next reconcile tick.

## Frontmatter fields

| Field | Required | Default | Description |
|---|---|---|---|
| `schedule` | yes | — | 5-field cron expression: `minute hour day-of-month month day-of-week`. |
| `schedule_timezone` | no | `America/Chicago` | IANA timezone string (e.g. `UTC`, `Europe/London`). |
| `voice` | no | `true` | `true`: send TTS voice note + text. `false`: text only. |
| `notify` | no | `true` | `false`: run for side effects only, no Telegram delivery. |

## Cron quick reference

```
*    *    *    *    *
│    │    │    │    │
│    │    │    │    └── day of week (0–6, Sunday = 0)
│    │    │    └─────── month (1–12)
│    │    └──────────── day of month (1–31)
│    └───────────────── hour (0–23)
└────────────────────── minute (0–59)
```

| Cron | Fires |
|---|---|
| `0 7 * * *` | every day at 07:00 |
| `0 13 * * 1-5` | weekdays at 13:00 |
| `*/15 * * * *` | every 15 minutes |
| `0 9 * * 1` | every Monday at 09:00 |
| `0 0 1 * *` | first day of every month at 00:00 |
| `0 0 13 7 *` | every July 13th at 00:00 |

## Removing a scheduled task

Either delete the `schedule:` field from the skill's frontmatter (skill stays available for manual invocation) or delete the skill directory entirely. The next reconcile (within 30s) unschedules the job automatically.

## Multi-mind

Any mind under `minds/` can own scheduled skills. The scheduler walks every mind, so adding a new mind with its own scheduled skills "just works" — no scheduler-side configuration. Each fire targets the owning mind's container via the gateway's `mind_id` routing.

## Delivery surface

Today the scheduler delivers all responses to the chat ID configured as `telegram_owner_chat_id` in `config.yaml`, using the main `TELEGRAM_BOT_TOKEN`. Per-mind delivery surfaces are not yet supported.
