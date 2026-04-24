# Build HiveTools — MCP to FastAPI REST migration

**Card ID:** 1760363029599356212

## Description

Implement HiveTools as a standalone FastAPI service replacing hive_mind_mcp. This is a complete migration from the MCP (Model Context Protocol) architecture to a modern FastAPI REST API with bearer token authentication, Human-in-the-Loop (HITL) approval gating, and a management UI for tool inventory and approval workflows.

The work is specified in detail at `/mnt/dev/spark_to_bloom/src/backlog/mcp-migration.md`.

## Architecture Overview

HiveTools serves as a dedicated tool execution gateway between Claude minds (Ada, Bob, Bilby, Nagatha) and external services. It replaces the MCP protocol with FastAPI REST endpoints, adding:

- **Bearer token authentication** for API access by minds
- **HITL approval gates** on sensitive operations (browser automation, email sends, form submissions)
- **Sudo mode** for time-limited auto-approval windows after Daniel approves once
- **Management UI** for token management, HITL settings, and approval workflows
- **SQLite persistence** with five normalized tables

## Acceptance Criteria

- [x] Understand the specification at /mnt/dev/spark_to_bloom/src/backlog/mcp-migration.md
- [ ] Complete Task 1: Copy hive_mind_mcp → hive-tools/, strip MCP protocol, add FastAPI skeleton + bearer token auth
- [ ] Complete Task 2: Copy Spark to Bloom auth.py, wire login UI (rename cookie to `ht_session`, salt to `hive-tools-session`)
- [ ] Complete Task 3: Implement HITL tables, gate dependency, polling endpoint, Telegram notify
- [ ] Complete Task 4: Build management UI (tool inventory with HITL mode selectors, token management)
- [ ] Complete Task 5: Migrate all tools from hive_mind_mcp as FastAPI endpoints
- [ ] All tools functional and accessible via bearer token auth
- [ ] HITL approval flow (create → notify → poll → approve/deny/timeout) working end-to-end
- [ ] Management UI accessible and fully functional
- [ ] Database schema initialized on startup

## Tasks

### Task 1: FastAPI Foundation & Bearer Token Auth

Copy `hive_mind_mcp` → `hive-tools/` directory structure, then:

1. Create basic FastAPI app skeleton (main.py or server.py)
2. Implement `require_api_token()` dependency:
   - Parses `Authorization: Bearer <token>` header
   - Looks up token_hash in api_tokens table
   - Returns caller mind name
   - Raises 401 if invalid/revoked
3. Implement token generation (secrets.token_urlsafe(32) → SHA-256 hash storage)
4. Initialize database with all five tables (users, api_tokens, tool_hitl_settings, hitl_requests, sudo_grants)
5. Wire basic health check endpoint (GET /health)

### Task 2: Authentication UI & Session Management

1. Copy Spark to Bloom `/mnt/dev/spark_to_bloom/src/auth.py` verbatim into hive-tools/
2. Modify auth.py constants:
   - `SESSION_COOKIE_NAME = "ht_session"`
   - Salt in `_make_serializer()`: `"hive-tools-session"`
   - DB path env var: `HT_DB_PATH` (default: `data/hivetools.db`)
   - Secret key env var: `HT_SECRET_KEY`
3. Implement login endpoint (POST /login):
   - Accept username/password
   - Verify against users table
   - Set `ht_session` cookie on success
   - Redirect to dashboard on success, return 401 on failure
4. Implement login page (GET /login):
   - Simple HTML form (username, password, submit)
5. Implement session dependency:
   - Parse `ht_session` cookie
   - Verify signature and expiry
   - Return user dict or None

### Task 3: HITL Gate & Approval Flow

1. Create HITL database tables:
   - `tool_hitl_settings(tool_name, mode, sudo_timeout_minutes)`
   - `hitl_requests(id, tool_name, caller_mind, params_json, status, created_at, expires_at, decided_at)`
   - `sudo_grants(id, tool_name, mind_name, granted_at, expires_at)`
2. Implement `hitl_gate(tool_name)` dependency:
   - Check tool_hitl_settings.mode
   - If `off`: pass through immediately
   - If `sudo`: check for unexpired sudo_grant; if valid, pass through
   - If `on` or `sudo` without valid grant: create hitl_request, notify Daniel, raise 202
3. Implement polling endpoint (GET /hitl/{request_id}):
   - Return 200 with cached result if decided
   - Return 202 with request status if still pending
   - Return 408 if expires_at passed
   - Return 403 if denied
4. Implement Telegram notification:
   - Send to Daniel with tool name, caller mind, params summary
   - Include approve/deny inline buttons
5. Implement HITL response handler (POST /hitl/{request_id}/respond):
   - Accept action (approve|deny) from Telegram webhook
   - Update hitl_requests.status and decided_at
   - If mode == "sudo" and action == "approve": create sudo_grant with timeout
   - Return 200

### Task 4: Management UI

Build a simple dashboard with Flask/Jinja2 templates or FastAPI Jinja2 response:

**Routes:**
- `GET /` — Dashboard (authenticated)
  - Recent HITL approvals/denials
  - Active sudo grants
  - Token usage stats
- `GET /tools` — Tool inventory
  - Auto-discover from app.routes
  - One row per tool (name, method, default HITL mode)
  - Radio buttons: Off / On / Sudo (+ timeout input)
  - Save button
- `GET /tokens` — Token management
  - List existing tokens (name, created_at, last_used_at, revoked_at)
  - "Create new token" form
  - Revoke button per token
- `GET /hitl` — Pending approvals
  - List pending hitl_requests
  - Show tool_name, caller_mind, params_json (formatted)
  - Approve/Deny buttons
  - Auto-refresh every 5 seconds

**Tool inventory auto-discovery:**
- Iterate `app.routes`
- Filter by FastAPI routes (exclude static, etc.)
- Extract path (e.g., `/browser/navigate` → tool_name = `browser_navigate`)
- Lookup tool_hitl_settings; show current mode
- On save: update database

### Task 5: Tool Migration

Migrate tools from `/mnt/dev/hive_mind_mcp/tools/`:

| Source | New Endpoint | Method | Default HITL |
|---|---|---|---|
| `calendar.py` — list events | `GET /calendar/events` | GET | off |
| `gmail.py` — list messages | `GET /gmail/messages` | GET | off |
| `gmail.py` — send message | `POST /gmail/send` | POST | on |
| `linkedin.py` — post | `POST /linkedin/post` | POST | on |
| `docker_ops.py` — all ops | `POST /docker/ops` | POST | on |
| `browser.py` (if available) — navigate/click/read | `POST /browser/navigate`, `POST /browser/click`, `POST /browser/read` | POST | sudo |
| `browser.py` — form submit | `POST /browser/submit` | POST | on |

**For each tool:**
1. Strip MCP protocol wrappers
2. Convert to FastAPI endpoint with request/response models
3. Wire `caller_mind: str = Depends(hitl_gate("tool_name"))` parameter
4. Use caller_mind for audit/logging
5. Return structured JSON response
6. Preserve original tool logic; focus on API translation

## Database Schema

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    disabled_at INTEGER
);

CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at INTEGER
);

CREATE TABLE tool_hitl_settings (
    tool_name TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'off',
    sudo_timeout_minutes INTEGER NOT NULL DEFAULT 15
);

CREATE TABLE hitl_requests (
    id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    caller_mind TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    decided_at INTEGER
);

CREATE TABLE sudo_grants (
    id INTEGER PRIMARY KEY,
    tool_name TEXT NOT NULL,
    mind_name TEXT NOT NULL,
    granted_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
```

## Work Order (Priority)

1. Copy hive_mind_mcp → hive-tools/, strip MCP protocol, add FastAPI skeleton + bearer token auth
2. Copy Spark to Bloom auth.py, wire login UI
3. Implement HITL tables, gate dependency, polling endpoint, Telegram notify
4. Build management UI — tool inventory with HITL mode selectors, token management
5. Migrate each tool from hive_mind_mcp as a FastAPI endpoint

## Key Dependencies

- **FastAPI** (framework)
- **SQLite3** (stdlib; database)
- **secrets** (stdlib; token generation)
- **hashlib** (stdlib; SHA-256)
- **itsdangerous** or bcrypt (from Spark to Bloom auth.py)
- **Telegram API** (for HITL notifications)
- **Google APIs** (calendar, gmail — from existing tools)

## Testing Considerations

- Bearer token auth with valid/invalid/revoked tokens
- HITL gate behavior: off → pass, on → 202, sudo → conditional
- Approval polling: pending → 202, approved → 200, denied → 403, timeout → 408
- Sudo grant window enforcement
- Tool endpoints accessible after auth
- Management UI CSRF protection (if applicable)
- Token generation uniqueness

## Notes

- Do NOT implement any other pipeline steps; focus solely on the HiveTools service
- All tool logic remains the same; this is a transport layer migration
- The MCP protocol is being replaced with REST; internal tool implementations stay mostly unchanged
- Session cookies are httponly, samesite=lax, secure=True
- Tokens are stored only as SHA-256 hashes; raw token shown once at creation
- Telegram webhook integration for HITL notifications (requires existing bot token)
