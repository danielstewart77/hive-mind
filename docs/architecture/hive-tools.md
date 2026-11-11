# Hive Tools

## What it is

`hive-tools` is a separate FastAPI service (`http://hive-tools:9421`) that provides authenticated REST access to external integrations: Gmail, Google Calendar, Docker compose control, browser automation, and HITL approval UI.

It runs outside the Hive Mind container stack and is not mounted into any mind container. Access is via HTTP only.

## Why it exists

External integrations (Gmail, Calendar, browser, Docker ops, HITL) live outside the mind containers behind a bearer-gated REST API so any compromised mind can only reach them via authenticated network calls — no direct in-process access, no shared memory.

## Authentication

All endpoints require `Authorization: Bearer <token>`.

Token is stored in keyring: `python3 -m keyring get hive-mind "HIVE_TOOLS_TOKEN"`

In skills:
```bash
HIVE_TOOLS_TOKEN=$(/opt/venv/bin/python3 -m keyring get hive-mind "HIVE_TOOLS_TOKEN" 2>/dev/null)
```

## Key Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /calendar/events` | Fetch calendar events |
| `GET /calendar/availability` | Check availability |
| `POST /calendar/events/quick-add` | Create calendar event |
| `GET /gmail/messages` | Search/read email |
| `POST /gmail/send` | Send email |
| `POST /gmail/reply` | Reply to email |
| `GET /docker/status?path=<host-path>` | Compose service status |
| `POST /docker/restart` | Restart a service |
| `GET /hitl/{id}` | Check HITL approval status |
| `POST /hitl/{id}/respond` | Approve or deny — approver credential only (holder `hitl-approver`, held by the Telegram surface as `HIVE_TOOLS_APPROVER_TOKEN`); a mind's token gets 403 |

hive-tools serves no OpenAPI schema (`/openapi.json` is disabled); the routers under `tools/` in the hive-tools repo are the reference.

## Anti-patterns

- Retrieve the token via `python3 -m keyring get`, not a hardcoded value anywhere in a skill or script
- Do not hardcode the token value anywhere
