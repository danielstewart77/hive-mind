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
| `memory_store` | No | Store a semantic memory (vector embedding in Neo4j) |
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

### Planka (Kanban)

| Tool | HITL | Description |
|---|---|---|
| `planka_list_projects` | No | List all Planka projects |
| `planka_get_board` | No | Get board with all lists and cards |
| `planka_get_card` | No | Get card details by ID |
| `planka_create_card` | No | Create a new card in a list |
| `planka_move_card` | No | Move a card to a different list |
| `planka_update_card` | No | Update card name or description |
| `planka_add_comment` | No | Add a comment to a card |
| `planka_assign_label` | No | Assign a label to a card |

### Notifications & Reminders

| Tool | HITL | Description |
|---|---|---|
| `notify_owner` | No | Send notification via Telegram, email, and/or file |
| `send_voice_message` | No | Send a TTS voice message via Telegram |
| `set_reminder` | No | Set a one-time reminder (fires via scheduler) |
| `list_reminders` | No | List all pending reminders |
| `delete_reminder` | No | Delete a reminder by ID |
| `get_due_reminders` | No | Get reminders that are currently due |

### Secrets

| Tool | HITL | Description |
|---|---|---|
| `set_secret` | No | Store a secret in the system keyring |
| `get_secret` | No | Retrieve a secret from the keyring |
| `list_secrets` | No | List all stored secret key names (not values) |

### Self-Improvement

| Tool | HITL | Description |
|---|---|---|
| `create_tool` | No | Write, AST-validate, and register a new MCP tool in `agents/` |
| `install_dependency` | No | Install a Python package into the venv |

### Social & Market Data

| Tool | HITL | Description |
|---|---|---|
| `search_x_threads` | No | Search X (Twitter) threads by keyword |
| `get_x_thread_replies` | No | Get replies and engagement for an X thread |
| `get_crypto_price` | No | Get current price for a cryptocurrency |

### Utilities

| Tool | HITL | Description |
|---|---|---|
| `get_current_time` | No | Current time in a given timezone (default: America/Chicago) |
| `get_weather_for_location` | No | Weather forecast for a location and time span |
| `agent_logs` | No | Read recent logs from hive_mind container services |
| `code_genius` | No | Invoke the code-genius coding skill from a documents path |
| `planning_genius` | No | Invoke the planning-genius skill from a documents path |

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

**Internal tool** (no external auth, no Docker socket needed):
→ Create `agents/my_tool.py` with `@tool()` decorator. Auto-discovered immediately, no restart needed.

**External tool** (OAuth, file credentials, Docker access):
→ Create `hive_mind_mcp/tools/my_tool.py`, import and register in `hive_mind_mcp/server.py`, restart the `hive-mind-mcp` container.

See [external-mcp.md](external-mcp.md) for the full pattern.

---

