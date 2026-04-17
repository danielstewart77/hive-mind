# MCP Tool Reference

Hive Mind exposes tools across two MCP servers. Both are available to Ada in every session.

- **`hive-mind-tools`** — internal, stdio, spawned by Claude CLI (`mcp_server.py`)
- **`hive-mind-mcp`** — external, SSE, separate Docker service (`hive_mind_mcp/`)

HITL = requires human approval via Telegram before executing. See [hitl-approval.md](../../specs/hitl-approval.md).

---

## hive-mind-tools (Internal)

### Memory

| Tool | HITL | Description |
|---|---|---|
| `memory_store` | No | Store a semantic memory (vector embedding in Lucent/SQLite) |
| `memory_retrieve` | No | Retrieve memories by semantic similarity |
| `memory_list` | No | Paginate through all stored memories |
| `memory_update` | No | Update data class or tags on an existing memory |
| `memory_delete` | No | Delete a memory by ID |

### Knowledge Graph

| Tool | HITL | Description |
|---|---|---|
| `graph_upsert` | No | Create or update an entity node with relationships |
| `graph_query` | No | Query the graph by entity name (fuzzy match) |
| `search_person` | No | Search persons by first name, last name, title, or relationship |

---

## Stateless Tools (via Skills)

The following capabilities are no longer MCP tools. They are standalone scripts in `tools/stateless/`, each with its own venv, invoked via Claude skills.

| Capability | Skill | Script |
|---|---|---|
| Weather | `/weather` | `tools/stateless/weather/weather.py` |
| Crypto prices | `/crypto-price` | `tools/stateless/crypto/crypto.py` |
| Current time | `/current-time` | `tools/stateless/current_time/current_time.py` |
| X/Twitter search | `/x-search` | `tools/stateless/x_api/x_api.py` |
| Notifications | `/notify` | `tools/stateless/notify/notify.py` |
| Reminders | `/reminders` | `tools/stateless/reminders/reminders.py` |
| Secrets | `/secrets` | `tools/stateless/secrets/secrets.py` |
| Planka Kanban | `/planka` | `tools/stateless/planka/planka.py` |
| Agent logs | `/agent-logs` | `tools/stateless/agent_logs/agent_logs.py` |

See `specs/tool-migration.md` for the migration rationale and pattern details.

---

## hive-mind-mcp (External)

### Gmail

| Tool | HITL | Description |
|---|---|---|
| `read_emails` | No | List emails from inbox or a label |
| `get_email` | No | Get a specific email by ID |
| `list_labels` | No | List all Gmail labels |
| `send_email` | Yes | Send an email |
| `reply_to_email` | Yes | Reply to an email thread |
| `delete_email` | Yes | Delete an email |
| `move_email` | Yes | Move an email to a label |

### Google Calendar

| Tool | HITL | Description |
|---|---|---|
| `list_calendar_events` | No | List events across calendars for a date range |
| `get_calendar_event` | No | Get a specific event by ID |
| `list_calendars` | No | List all available calendars |
| `check_availability` | No | Check free/busy for a time range |
| `create_calendar_event` | Yes | Create a new event |
| `quick_add_event` | Yes | Create an event from natural language text |
| `update_calendar_event` | Yes | Update an existing event |
| `delete_calendar_event` | Yes | Delete an event |
| `invite_to_event` | Yes | Add an attendee to an event |

### LinkedIn

| Tool | HITL | Description |
|---|---|---|
| `post_to_linkedin` | Yes | Post text content to Daniel's LinkedIn profile |

### Docker Compose

| Tool | HITL | Description |
|---|---|---|
| `compose_up` | Yes | Start a Docker Compose project |
| `compose_restart` | Yes | Restart a project or a specific service |
| `compose_down` | Yes | Stop a Docker Compose project |
| `compose_logs` | No | Fetch recent logs from a project or service |
| `compose_status` | No | Get running status of a project |
| `docker_list_containers` | No | List all Docker containers |
| `docker_list_networks` | No | List Docker networks |

---

## Adding New Tools

Use the `/tool-creator` skill. It reads `specs/tool-migration.md` to determine the right pattern:

**Stateless tool** (API call, file op, no persistent connection):
→ Creates `tools/stateless/<name>/` with script, `requirements.txt`, venv, and a Claude skill. Editable without restart.

**Stateful tool** (needs Lucent, Playwright, or other persistent connection):
→ Adds a function to `tools/stateful/` and registers it in `mcp_server.py`. Requires `hive_mind` container restart.

**External tool** (OAuth, file credentials, Docker access):
→ Create in `hive_mind_mcp/`, register in `hive_mind_mcp/server.py`, restart `hive-mind-mcp` container.

See [external-mcp.md](external-mcp.md) for the external pattern.

---

