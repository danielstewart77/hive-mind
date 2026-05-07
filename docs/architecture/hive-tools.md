# Hive Tools

## What it is

`hive-tools` is a separate FastAPI service (`http://hive-tools:9421`) that provides authenticated REST access to external integrations: Gmail, Google Calendar, Docker compose control, browser automation, and HITL approval UI.

It runs outside the Hive Mind container stack and is not mounted into any mind container. Access is via HTTP only.

## Why it exists

MCP was removed from all skills. Skills previously called `mcp__hive-mind-mcp__*` tools directly. Those tools are now REST endpoints in hive-tools, called with a bearer token.

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
| `POST /hitl/{id}/respond` | Respond to HITL request |

Full schema at `http://hive-tools:9421/openapi.json`.

## Anti-patterns

- Do not call `mcp__hive-mind-mcp__*` tools in skills — MCP is removed
- Do not use `secrets.py get` to retrieve the token — it has no `get` subcommand; use `python3 -m keyring get` directly
- Do not hardcode the token value anywhere
