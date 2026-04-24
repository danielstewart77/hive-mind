# Implementation Plan: 1760363029599356212 - Build HiveTools (MCP to FastAPI REST Migration)

## Overview

HiveTools is a standalone FastAPI service that replaces `hive_mind_mcp`. It migrates all existing MCP tools (Calendar, Gmail, LinkedIn, Docker Ops) from the MCP protocol to REST endpoints with bearer token authentication, a HITL (Human-in-the-Loop) approval gate dependency, and a management UI for tool inventory, token management, and approval workflows. The service lives at `/mnt/dev/hive-tools/` with its own Docker setup, SQLite database, and test suite.

## Technical Approach

- **FastAPI skeleton** with two auth surfaces: bearer tokens for minds (API), signed-cookie sessions for Daniel (management UI). This mirrors the existing `hive_mind_mcp` bearer auth + `spark_to_bloom` session auth patterns.
- **HITL gate as a FastAPI dependency** (`hitl_gate(tool_name)`) that chains after `require_api_token()`. The gate checks `tool_hitl_settings` mode and either passes through, checks sudo grants, or creates a pending HITL request and raises HTTP 202.
- **Management UI** uses Jinja2 templates (same pattern as Spark to Bloom's `templates/login.html`).
- **Tool migration** preserves all existing tool logic from `hive_mind_mcp/tools/` files, stripping MCP protocol wrappers and the old `require_approval()` polling pattern, replacing them with FastAPI request/response models and the new `hitl_gate` dependency.
- **SQLite database** with five tables initialized on startup via `init_db()`.
- **Test suite** follows pytest conventions from hive_mind's test structure: unit tests for pure logic, API tests for endpoints via `TestClient`.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|-------------|-------|
| Bearer token auth middleware | `/mnt/dev/hive_mind_mcp/server.py` (BearerAuthMiddleware) | Adapted to FastAPI dependency `require_api_token()` with DB hash lookup |
| Session auth (cookie-based) | `/mnt/dev/spark_to_bloom/src/auth.py` | Copied and adapted: cookie name `ht_session`, salt `hive-tools-session`, env vars `HT_DB_PATH`/`HT_SECRET_KEY` |
| MCP tool HITL approval | `/mnt/dev/hive_mind_mcp/tools/approval.py` | Replaced by `hitl_gate()` FastAPI dependency (no more gateway polling) |
| Calendar tools | `/mnt/dev/hive_mind_mcp/tools/calendar.py` | Migrated to FastAPI endpoints, stripped `require_approval()` calls |
| Gmail tools | `/mnt/dev/hive_mind_mcp/tools/gmail.py` | Migrated to FastAPI endpoints, stripped `require_approval()` calls |
| LinkedIn tools | `/mnt/dev/hive_mind_mcp/tools/linkedin.py` | Migrated to FastAPI endpoint |
| Docker ops tools | `/mnt/dev/hive_mind_mcp/tools/docker_ops.py` | Migrated to FastAPI endpoints |
| API test fixtures | `/usr/src/app/tests/api/test_broker_endpoints.py` | Pattern for TestClient fixtures with tmp_path DB |
| Unit test fixtures | `/usr/src/app/tests/unit/test_broker.py` | Pattern for SQLite-backed unit tests |
| Telegram notifications | `/usr/src/app/core/notify_utils.py` | Pattern for direct Telegram API calls (adapted for HITL notifications) |

## Models & Schemas

All Pydantic models in `/mnt/dev/hive-tools/schemas.py`:

```python
# --- API Token Management ---
class TokenCreateRequest(BaseModel):
    name: str  # mind name, e.g. "ada"

class TokenCreateResponse(BaseModel):
    name: str
    raw_token: str  # shown once, never again
    created_at: int

class TokenInfo(BaseModel):
    id: int
    name: str
    created_at: int
    last_used_at: int | None
    revoked_at: int | None

# --- HITL ---
class HITLRequestResponse(BaseModel):
    hitl_request_id: str
    status: str  # "pending"

class HITLStatusResponse(BaseModel):
    status: str  # "pending" | "approved" | "denied"
    tool_name: str
    caller_mind: str
    decided_at: int | None

class HITLRespondRequest(BaseModel):
    action: str  # "approve" | "deny"

# --- Tool Settings ---
class ToolSettingUpdate(BaseModel):
    mode: str  # "off" | "on" | "sudo"
    sudo_timeout_minutes: int = 15

class ToolInfo(BaseModel):
    tool_name: str
    method: str
    path: str
    mode: str
    sudo_timeout_minutes: int

# --- Login ---
class LoginRequest(BaseModel):
    username: str
    password: str

# --- Gmail endpoint models ---
class GmailReadRequest(BaseModel):
    query: str
    max_results: int = 10

class GmailSendRequest(BaseModel):
    to: str
    subject: str
    body: str
    cc: str = ""

# --- Calendar endpoint models ---
class CalendarListRequest(BaseModel):
    query: str = ""
    time_min: str = ""
    time_max: str = ""
    max_results: int = 25
    calendar_id: str = "primary"

class CalendarCreateRequest(BaseModel):
    summary: str
    start_time: str
    end_time: str
    description: str = ""
    location: str = ""
    attendees: str = ""
    timezone: str = "America/Chicago"
    calendar_id: str = "primary"

# --- LinkedIn ---
class LinkedInPostRequest(BaseModel):
    content: str
    image_path: str = ""

# --- Docker Ops ---
class DockerOpsRequest(BaseModel):
    operation: str  # "up" | "down" | "restart" | "logs" | "status"
    project: str
    service: str = ""
    tail: int = 50
```

## Implementation Steps

Each step: write test first, then implement to pass. Tests assert observable behavior only (return values, API responses, raised exceptions, DB state).

**Important:** This service lives at `/mnt/dev/hive-tools/`, NOT inside the hive_mind repo. All file paths below are relative to `/mnt/dev/hive-tools/`.

---

### Step 1: Project Scaffolding & Database Schema

**Files:**
- Create: `/mnt/dev/hive-tools/db.py` -- database initialization and connection helpers
- Create: `/mnt/dev/hive-tools/config.py` -- configuration constants (DB path, secret key, etc.)
- Create: `/mnt/dev/hive-tools/requirements.txt` -- dependencies
- Create: `/mnt/dev/hive-tools/tests/__init__.py`
- Create: `/mnt/dev/hive-tools/tests/conftest.py` -- shared fixtures (tmp_path DB)

**Test First (unit):** `tests/unit/test_db.py`
- [ ] `test_init_db_creates_all_five_tables` -- asserts `users`, `api_tokens`, `tool_hitl_settings`, `hitl_requests`, `sudo_grants` tables exist after `init_db()`
- [ ] `test_init_db_idempotent` -- calling `init_db()` twice does not raise
- [ ] `test_init_db_creates_directory` -- if parent dir does not exist, `init_db()` creates it

**Then Implement:**
- [ ] Create `config.py` with constants: `DB_PATH = os.getenv("HT_DB_PATH", "data/hivetools.db")`, `SECRET_KEY = os.getenv("HT_SECRET_KEY", "change-this-in-production")`, `BASE_DIR = Path(__file__).resolve().parent`
- [ ] Create `db.py` with `init_db(db_path=None) -> sqlite3.Connection` that creates all five tables per the schema in the story description. Use `CREATE TABLE IF NOT EXISTS`. Set `conn.row_factory = sqlite3.Row`. Return the connection.
- [ ] Create `get_db(db_path=None) -> sqlite3.Connection` helper that calls `init_db` then returns a connection.
- [ ] Create `requirements.txt`: `fastapi`, `uvicorn[standard]`, `jinja2`, `itsdangerous`, `bcrypt`, `aiohttp`, `httpx`, `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `python-multipart`

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/unit/test_db.py -v`

---

### Step 2: Pydantic Schemas

**Files:**
- Create: `/mnt/dev/hive-tools/schemas.py` -- all request/response models

**Test First (unit):** `tests/unit/test_schemas.py`
- [ ] `test_token_create_request_requires_name` -- asserts ValidationError when `name` missing
- [ ] `test_hitl_respond_request_validates_action` -- asserts model accepts "approve" and "deny"
- [ ] `test_tool_setting_update_defaults` -- asserts `sudo_timeout_minutes` defaults to 15
- [ ] `test_gmail_send_request_cc_optional` -- asserts model accepts without `cc` field

**Then Implement:**
- [ ] Create `schemas.py` with all Pydantic models listed in the Models & Schemas section above

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/unit/test_schemas.py -v`

---

### Step 3: Bearer Token Auth Dependency

**Files:**
- Create: `/mnt/dev/hive-tools/auth_api.py` -- `require_api_token()` dependency and token management functions
- Modify: `/mnt/dev/hive-tools/db.py` -- add token-related DB helpers if needed

**Test First (unit):** `tests/unit/test_auth_api.py`
- [ ] `test_generate_token_returns_raw_and_stores_hash` -- asserts `generate_token(db, "ada")` returns a raw token string, and the DB contains its SHA-256 hash
- [ ] `test_generate_token_hash_is_sha256` -- asserts the stored hash matches `hashlib.sha256(raw_token.encode()).hexdigest()`
- [ ] `test_validate_token_returns_mind_name` -- asserts valid token returns `"ada"` from DB lookup
- [ ] `test_validate_token_invalid_returns_none` -- asserts invalid token returns None
- [ ] `test_validate_token_revoked_returns_none` -- asserts revoked token (with `revoked_at` set) returns None
- [ ] `test_validate_token_updates_last_used_at` -- asserts `last_used_at` is updated on successful validation
- [ ] `test_revoke_token_sets_revoked_at` -- asserts revoking a token sets `revoked_at` timestamp
- [ ] `test_list_tokens_returns_all` -- asserts listing returns all tokens (active and revoked)

**Test First (API):** `tests/api/test_bearer_auth.py`
- [ ] `test_endpoint_without_auth_returns_401` -- asserts request without Authorization header returns 401
- [ ] `test_endpoint_with_invalid_token_returns_401` -- asserts request with wrong token returns 401
- [ ] `test_endpoint_with_valid_token_returns_200` -- asserts request with valid token passes auth

**Then Implement:**
- [ ] Create `auth_api.py` with:
  - `generate_token(db, name: str) -> str` -- generates `secrets.token_urlsafe(32)`, stores SHA-256 hash in `api_tokens`, returns raw token
  - `validate_token(db, raw_token: str) -> str | None` -- hashes token, looks up in `api_tokens` where `revoked_at IS NULL`, updates `last_used_at`, returns `name` or None
  - `revoke_token(db, token_id: int) -> bool` -- sets `revoked_at`
  - `list_tokens(db) -> list[dict]` -- returns all tokens
  - `require_api_token(request: Request) -> str` -- FastAPI dependency that parses `Authorization: Bearer <token>`, calls `validate_token`, raises 401 if invalid, returns caller mind name. Gets DB from `request.app.state.db`.

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/unit/test_auth_api.py tests/api/test_bearer_auth.py -v`

---

### Step 4: Session Auth (Management UI)

**Files:**
- Create: `/mnt/dev/hive-tools/auth_session.py` -- adapted from Spark to Bloom's `auth.py`

**Test First (unit):** `tests/unit/test_auth_session.py`
- [ ] `test_hash_password_returns_prefixed_string` -- asserts result starts with "bcrypt$" or "pbkdf2$"
- [ ] `test_hash_password_empty_raises` -- asserts ValueError on empty password
- [ ] `test_verify_password_correct` -- asserts `verify_password("pass", hash_password("pass"))` is True
- [ ] `test_verify_password_wrong` -- asserts `verify_password("wrong", hash_password("pass"))` is False
- [ ] `test_create_user_and_get_by_username` -- asserts user can be created and retrieved
- [ ] `test_create_user_duplicate_raises` -- asserts duplicate username raises IntegrityError
- [ ] `test_create_session_token_and_read_back` -- asserts `read_session_token(create_session_token(user))` returns payload with user_id
- [ ] `test_read_session_token_invalid_returns_none` -- asserts garbage token returns None
- [ ] `test_verify_user_credentials_valid` -- asserts valid username/password returns user dict
- [ ] `test_verify_user_credentials_disabled_returns_none` -- asserts disabled user returns None
- [ ] `test_session_cookie_name_is_ht_session` -- asserts `SESSION_COOKIE_NAME == "ht_session"`

**Test First (API):** `tests/api/test_login.py`
- [ ] `test_get_login_page_returns_html` -- asserts GET /login returns 200 with HTML content
- [ ] `test_post_login_valid_sets_cookie` -- asserts POST /login with valid credentials sets `ht_session` cookie and redirects
- [ ] `test_post_login_invalid_returns_401` -- asserts POST /login with bad credentials returns 401
- [ ] `test_authenticated_route_without_session_redirects` -- asserts GET / without session cookie redirects to /login

**Then Implement:**
- [ ] Copy `/mnt/dev/spark_to_bloom/src/auth.py` to `/mnt/dev/hive-tools/auth_session.py`
- [ ] Modify constants: `SESSION_COOKIE_NAME = "ht_session"`, salt in `_make_serializer()` to `"hive-tools-session"`
- [ ] Replace `from config import BASE_DIR, SECRET_KEY` with imports from local `config.py`
- [ ] Change `_db_path()` to use `os.getenv("HT_DB_PATH", "data/hivetools.db")`
- [ ] Change `_secret_key()` to use `os.getenv("HT_SECRET_KEY", ...)`
- [ ] Keep all functions: `hash_password`, `verify_password`, `create_user`, `get_user_by_username`, `get_user_by_id`, `verify_user_credentials`, `create_session_token`, `read_session_token`, `get_current_user_from_request`, `require_auth`, `set_session_cookie`, `clear_session_cookie`

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/unit/test_auth_session.py tests/api/test_login.py -v`

---

### Step 5: HITL Gate Dependency

**Files:**
- Create: `/mnt/dev/hive-tools/hitl.py` -- HITL gate dependency, request creation, notification, polling logic

**Test First (unit):** `tests/unit/test_hitl.py`
- [ ] `test_hitl_gate_mode_off_passes_through` -- asserts when mode is "off", gate returns caller_mind without creating a request
- [ ] `test_hitl_gate_mode_on_creates_request_and_raises_202` -- asserts when mode is "on", gate inserts a row in `hitl_requests` and raises HTTPException 202
- [ ] `test_hitl_gate_mode_sudo_with_valid_grant_passes_through` -- asserts when mode is "sudo" and an unexpired sudo_grant exists, gate passes through
- [ ] `test_hitl_gate_mode_sudo_without_grant_creates_request` -- asserts when mode is "sudo" with no grant, gate creates a request and raises 202
- [ ] `test_hitl_gate_mode_sudo_with_expired_grant_creates_request` -- asserts when mode is "sudo" with expired grant, gate creates a request
- [ ] `test_hitl_gate_no_setting_defaults_to_off` -- asserts when tool has no row in `tool_hitl_settings`, behavior is "off" (pass through)
- [ ] `test_hitl_request_has_uuid_id` -- asserts created request ID is a valid UUID
- [ ] `test_hitl_request_expires_in_5_minutes` -- asserts `expires_at` is approximately `created_at + 300`

**Test First (API):** `tests/api/test_hitl_endpoints.py`
- [ ] `test_get_hitl_status_pending_returns_202` -- asserts GET /hitl/{id} for pending request returns 202
- [ ] `test_get_hitl_status_approved_returns_200` -- asserts GET /hitl/{id} for approved request returns 200 with result
- [ ] `test_get_hitl_status_denied_returns_403` -- asserts GET /hitl/{id} for denied request returns 403
- [ ] `test_get_hitl_status_expired_returns_408` -- asserts GET /hitl/{id} for expired pending request returns 408
- [ ] `test_get_hitl_status_unknown_id_returns_404` -- asserts GET /hitl/{nonexistent} returns 404
- [ ] `test_post_hitl_respond_approve_updates_status` -- asserts POST /hitl/{id}/respond with action=approve sets status to "approved"
- [ ] `test_post_hitl_respond_deny_updates_status` -- asserts POST /hitl/{id}/respond with action=deny sets status to "denied"
- [ ] `test_post_hitl_respond_approve_sudo_creates_grant` -- asserts POST /hitl/{id}/respond for sudo-mode tool creates a sudo_grant row
- [ ] `test_post_hitl_respond_already_resolved_returns_409` -- asserts responding to already-resolved request returns 409

**Then Implement:**
- [ ] Create `hitl.py` with:
  - `hitl_gate(tool_name: str)` factory returning a FastAPI dependency `_check(request, caller_mind)` that:
    1. Gets DB from `request.app.state.db`
    2. Looks up `tool_hitl_settings` for `tool_name`; defaults to `mode="off"` if not found
    3. If `mode == "off"`: returns `caller_mind`
    4. If `mode == "sudo"`: checks `sudo_grants` for unexpired grant; if found, returns `caller_mind`
    5. Otherwise: creates `hitl_requests` row (UUID id, 300s expiry), calls `notify_daniel()`, raises `HTTPException(status_code=202, detail={"hitl_request_id": request_id, "status": "pending"})`
  - `get_hitl_status(db, request_id: str) -> dict` -- returns status dict with computed state
  - `respond_to_hitl(db, request_id: str, action: str) -> dict` -- updates status, optionally creates sudo_grant
  - `notify_daniel(tool_name, caller_mind, params, request_id)` -- sends Telegram notification (pattern from `core/notify_utils.py`). Uses `httpx` with inline keyboard buttons (pattern from `specs/hitl-telegram-inline-buttons.md`).

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/unit/test_hitl.py tests/api/test_hitl_endpoints.py -v`

---

### Step 6: FastAPI App Skeleton & Health Check

**Files:**
- Create: `/mnt/dev/hive-tools/server.py` -- main FastAPI application, lifespan, route registration
- Create: `/mnt/dev/hive-tools/tools/__init__.py`

**Test First (API):** `tests/api/test_app.py`
- [ ] `test_health_endpoint_returns_200` -- asserts GET /health returns `{"status": "ok"}`
- [ ] `test_app_initializes_db_on_startup` -- asserts `app.state.db` is set after lifespan start
- [ ] `test_openapi_schema_available` -- asserts GET /openapi.json returns valid schema

**Then Implement:**
- [ ] Create `server.py` with:
  - FastAPI app with `lifespan` that calls `init_db()`, stores connection on `app.state.db`, creates default admin user if users table is empty
  - `GET /health` endpoint returning `{"status": "ok"}`
  - Mount Jinja2 templates directory
  - `GET /login` and `POST /login` endpoints (delegating to `auth_session.py`)
  - `GET /hitl/{request_id}` polling endpoint
  - `POST /hitl/{request_id}/respond` response endpoint
  - Import and include tool routers (added in later steps)
  - Run with uvicorn when `__name__ == "__main__"` on port 9421

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/api/test_app.py -v`

---

### Step 7: Management UI -- Dashboard & Tool Inventory

**Files:**
- Create: `/mnt/dev/hive-tools/templates/layout.html` -- base template
- Create: `/mnt/dev/hive-tools/templates/login.html` -- login page
- Create: `/mnt/dev/hive-tools/templates/dashboard.html` -- main dashboard
- Create: `/mnt/dev/hive-tools/templates/tools.html` -- tool inventory with HITL mode selectors
- Create: `/mnt/dev/hive-tools/templates/tokens.html` -- token management
- Create: `/mnt/dev/hive-tools/templates/hitl.html` -- pending approvals
- Create: `/mnt/dev/hive-tools/ui.py` -- management UI route handlers

**Test First (API):** `tests/api/test_management_ui.py`
- [ ] `test_dashboard_requires_auth` -- asserts GET / without session redirects to /login
- [ ] `test_dashboard_with_auth_returns_html` -- asserts GET / with valid session returns 200 HTML
- [ ] `test_tools_page_lists_registered_routes` -- asserts GET /tools shows tool names from app.routes
- [ ] `test_tools_page_save_updates_hitl_mode` -- asserts POST /tools/settings updates `tool_hitl_settings` in DB
- [ ] `test_tokens_page_lists_tokens` -- asserts GET /tokens shows existing tokens
- [ ] `test_tokens_create_shows_raw_token_once` -- asserts POST /tokens/create returns page with raw token displayed
- [ ] `test_tokens_revoke_sets_revoked_at` -- asserts POST /tokens/{id}/revoke updates the token
- [ ] `test_hitl_page_lists_pending_requests` -- asserts GET /hitl shows pending hitl_requests
- [ ] `test_hitl_page_approve_button_works` -- asserts POST /hitl/{id}/ui-respond with action=approve updates status

**Then Implement:**
- [ ] Create `ui.py` with an `APIRouter` containing all management UI routes:
  - `GET /` -- dashboard (requires `require_auth` dependency). Shows recent HITL requests, active sudo grants, token usage. Renders `dashboard.html`.
  - `GET /tools` -- tool inventory. Auto-discovers routes from `request.app.routes`, filters to tool endpoints (those under `/calendar/`, `/gmail/`, `/linkedin/`, `/docker/`, `/browser/`). Looks up `tool_hitl_settings` for each. Renders `tools.html`.
  - `POST /tools/settings` -- saves HITL mode changes from form submission. Accepts form data with `tool_name`, `mode`, `sudo_timeout_minutes`. Upserts into `tool_hitl_settings`.
  - `GET /tokens` -- lists all API tokens. Renders `tokens.html`.
  - `POST /tokens/create` -- generates new token via `generate_token()`, renders page showing raw token once.
  - `POST /tokens/{token_id}/revoke` -- revokes token, redirects to /tokens.
  - `GET /hitl` -- lists pending HITL requests. Auto-refreshes every 5s via meta tag. Renders `hitl.html`.
  - `POST /hitl/{request_id}/ui-respond` -- approve/deny from UI, redirects back to /hitl page.
- [ ] Create templates following Spark to Bloom's `login.html` pattern (Jinja2 extends layout.html). `login.html` adapted from `/mnt/dev/spark_to_bloom/src/templates/login.html` with HiveTools branding.
- [ ] `tools.html` template: table with one row per tool, radio buttons for Off/On/Sudo, timeout input, save button.
- [ ] `tokens.html` template: table of tokens, create form, revoke buttons.
- [ ] `hitl.html` template: table of pending requests with approve/deny buttons, auto-refresh meta tag.

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/api/test_management_ui.py -v`

---

### Step 8: Migrate Calendar Tools

**Files:**
- Create: `/mnt/dev/hive-tools/tools/calendar.py` -- FastAPI router with all calendar endpoints

**Test First (API):** `tests/api/test_calendar.py`
- [ ] `test_list_events_requires_auth` -- asserts GET /calendar/events without bearer token returns 401
- [ ] `test_list_events_with_auth_calls_service` -- asserts GET /calendar/events with valid token calls Google Calendar API (mocked) and returns JSON with events array
- [ ] `test_list_events_hitl_mode_off_passes_through` -- asserts with tool_hitl_settings mode="off", request proceeds without HITL
- [ ] `test_get_event_returns_single_event` -- asserts GET /calendar/events/{id} returns event details
- [ ] `test_list_calendars_returns_array` -- asserts GET /calendar/list returns calendar array
- [ ] `test_create_event_hitl_on_returns_202` -- asserts POST /calendar/events with HITL mode "on" returns 202 with hitl_request_id
- [ ] `test_calendar_service_not_authenticated_returns_error` -- asserts correct error JSON when credentials are missing

**Test First (unit):** `tests/unit/test_calendar_service.py`
- [ ] `test_get_header_fields_extracts_all_fields` -- asserts `_get_header_fields()` correctly extracts event fields (ported from existing tool logic)
- [ ] `test_get_header_fields_handles_missing_fields` -- asserts graceful defaults for missing event fields

**Then Implement:**
- [ ] Create `tools/calendar.py` with an `APIRouter(prefix="/calendar", tags=["calendar"])` containing:
  - `GET /events` -- list/search events. Params: query, time_min, time_max, max_results, calendar_id. Dependency: `caller: str = Depends(hitl_gate("calendar_list_events"))`. Default HITL mode: off.
  - `GET /events/{event_id}` -- get single event. Dependency: `hitl_gate("calendar_get_event")`.
  - `GET /list` -- list calendars. Dependency: `hitl_gate("calendar_list")`.
  - `GET /availability` -- check free/busy. Dependency: `hitl_gate("calendar_check_availability")`.
  - `POST /events` -- create event. Dependency: `hitl_gate("calendar_create_event")`. Default HITL mode: on.
  - `POST /events/quick-add` -- quick add. Dependency: `hitl_gate("calendar_quick_add")`.
  - `PATCH /events/{event_id}` -- update event. Dependency: `hitl_gate("calendar_update_event")`.
  - `DELETE /events/{event_id}` -- delete event. Dependency: `hitl_gate("calendar_delete_event")`.
  - `POST /events/{event_id}/invite` -- invite attendees. Dependency: `hitl_gate("calendar_invite")`.
- [ ] Preserve `_get_calendar_service()` and `_get_header_fields()` from `/mnt/dev/hive_mind_mcp/tools/calendar.py`. Remove all `require_approval()` calls (HITL is now handled by the dependency).
- [ ] Return structured JSON (not JSON strings) -- FastAPI handles serialization.

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/api/test_calendar.py tests/unit/test_calendar_service.py -v`

---

### Step 9: Migrate Gmail Tools

**Files:**
- Create: `/mnt/dev/hive-tools/tools/gmail.py` -- FastAPI router with all Gmail endpoints

**Test First (API):** `tests/api/test_gmail.py`
- [ ] `test_read_emails_requires_auth` -- asserts GET /gmail/messages without token returns 401
- [ ] `test_read_emails_with_auth_returns_messages` -- asserts GET /gmail/messages returns JSON with messages array (mocked Gmail service)
- [ ] `test_read_emails_hitl_off` -- asserts read endpoints pass through HITL gate with mode off
- [ ] `test_get_email_returns_full_message` -- asserts GET /gmail/messages/{id} returns complete message
- [ ] `test_list_labels_returns_array` -- asserts GET /gmail/labels returns label array
- [ ] `test_send_email_hitl_on_returns_202` -- asserts POST /gmail/send with HITL mode "on" returns 202
- [ ] `test_reply_email_hitl_on_returns_202` -- asserts POST /gmail/reply with HITL mode "on" returns 202
- [ ] `test_delete_email_hitl_on_returns_202` -- asserts POST /gmail/trash with HITL mode "on" returns 202

**Test First (unit):** `tests/unit/test_gmail_service.py`
- [ ] `test_html_to_text_strips_tags` -- asserts `_html_to_text()` removes HTML tags
- [ ] `test_html_to_text_converts_links` -- asserts links become `text (url)` format
- [ ] `test_extract_body_prefers_plain_text` -- asserts plain text is preferred over HTML
- [ ] `test_extract_attachments_finds_files` -- asserts attachment metadata is extracted

**Then Implement:**
- [ ] Create `tools/gmail.py` with an `APIRouter(prefix="/gmail", tags=["gmail"])` containing:
  - `GET /messages` -- search/list emails. Params: query, max_results. Dependency: `hitl_gate("gmail_read_emails")`. Default HITL: off.
  - `GET /messages/{message_id}` -- get single email. Dependency: `hitl_gate("gmail_get_email")`.
  - `GET /labels` -- list labels. Dependency: `hitl_gate("gmail_list_labels")`.
  - `POST /send` -- send email. Body: GmailSendRequest. Dependency: `hitl_gate("gmail_send_email")`. Default HITL: on.
  - `POST /reply` -- reply to email. Dependency: `hitl_gate("gmail_reply_email")`. Default HITL: on.
  - `POST /trash` -- trash email. Dependency: `hitl_gate("gmail_delete_email")`. Default HITL: on.
  - `POST /move` -- modify labels. Dependency: `hitl_gate("gmail_move_email")`. Default HITL: on.
- [ ] Preserve all internal helpers (`_get_gmail_service`, `_html_to_text`, `_decode_body`, `_extract_body`, `_extract_attachments`, `_get_header`) from `/mnt/dev/hive_mind_mcp/tools/gmail.py`.
- [ ] Remove all `require_approval()` calls.

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/api/test_gmail.py tests/unit/test_gmail_service.py -v`

---

### Step 10: Migrate LinkedIn Tools

**Files:**
- Create: `/mnt/dev/hive-tools/tools/linkedin.py` -- FastAPI router with LinkedIn endpoint

**Test First (API):** `tests/api/test_linkedin.py`
- [ ] `test_post_linkedin_requires_auth` -- asserts POST /linkedin/post without token returns 401
- [ ] `test_post_linkedin_hitl_on_returns_202` -- asserts POST /linkedin/post with HITL mode "on" returns 202
- [ ] `test_post_linkedin_no_token_file_returns_error` -- asserts proper error when LinkedIn token file is missing (mocked)
- [ ] `test_post_linkedin_success_returns_post_id` -- asserts successful post returns post_id (fully mocked HTTP)

**Then Implement:**
- [ ] Create `tools/linkedin.py` with an `APIRouter(prefix="/linkedin", tags=["linkedin"])` containing:
  - `POST /post` -- post to LinkedIn. Body: LinkedInPostRequest. Dependency: `hitl_gate("linkedin_post")`. Default HITL: on.
- [ ] Preserve `_load_token`, `_save_token`, `_refresh_if_needed`, `_upload_image`, and `post_to_linkedin` logic from `/mnt/dev/hive_mind_mcp/tools/linkedin.py`.
- [ ] Remove `require_approval()` call.

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/api/test_linkedin.py -v`

---

### Step 11: Migrate Docker Ops Tools

**Files:**
- Create: `/mnt/dev/hive-tools/tools/docker_ops.py` -- FastAPI router with Docker ops endpoints

**Test First (API):** `tests/api/test_docker_ops.py`
- [ ] `test_docker_status_requires_auth` -- asserts GET /docker/status without token returns 401
- [ ] `test_docker_compose_up_hitl_on_returns_202` -- asserts POST /docker/up with HITL mode "on" returns 202
- [ ] `test_docker_compose_logs_returns_output` -- asserts GET /docker/logs returns log text (mocked subprocess)
- [ ] `test_docker_invalid_project_returns_error` -- asserts unknown project name returns error JSON
- [ ] `test_docker_mutate_blocked_project_returns_error` -- asserts mutating self (hive_tools) returns error
- [ ] `test_docker_list_containers_returns_array` -- asserts GET /docker/containers returns container list
- [ ] `test_docker_list_networks_returns_array` -- asserts GET /docker/networks returns network list

**Then Implement:**
- [ ] Create `tools/docker_ops.py` with an `APIRouter(prefix="/docker", tags=["docker"])` containing:
  - `POST /up` -- compose up. Params: project, service. Dependency: `hitl_gate("docker_compose_up")`. Default HITL: on.
  - `POST /restart` -- compose restart. Dependency: `hitl_gate("docker_compose_restart")`.
  - `POST /down` -- compose down. Dependency: `hitl_gate("docker_compose_down")`.
  - `GET /logs` -- compose logs (read-only). Params: project, service, tail. Dependency: `hitl_gate("docker_compose_logs")`. Default HITL: off.
  - `GET /status` -- compose status (read-only). Dependency: `hitl_gate("docker_compose_status")`. Default HITL: off.
  - `GET /containers` -- list all containers. Dependency: `hitl_gate("docker_list_containers")`. Default HITL: off.
  - `GET /networks` -- list networks. Dependency: `hitl_gate("docker_list_networks")`. Default HITL: off.
- [ ] Preserve `PROJECTS`, `MUTATE_BLOCKED` (update to include `hive_tools`), `_run`, `_validate_project`, `_compose_cmd`, `_result_json`, `_kills_session`, `_schedule_deferred` from `/mnt/dev/hive_mind_mcp/tools/docker_ops.py`.
- [ ] Remove all `require_approval()` calls. Add `hive_tools` to `MUTATE_BLOCKED` set.

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/api/test_docker_ops.py -v`

---

### Step 12: Default HITL Settings Seeding

**Files:**
- Modify: `/mnt/dev/hive-tools/db.py` -- add `seed_default_hitl_settings(db)` function
- Modify: `/mnt/dev/hive-tools/server.py` -- call seeder on startup after `init_db()`

**Test First (unit):** `tests/unit/test_hitl_defaults.py`
- [ ] `test_seed_creates_default_settings` -- asserts after seeding, all expected tool names have entries with correct default modes
- [ ] `test_seed_idempotent_does_not_overwrite` -- asserts calling seed twice does not overwrite user-modified settings (uses INSERT OR IGNORE)
- [ ] `test_default_modes_match_spec` -- asserts calendar read tools are "off", gmail send is "on", browser navigate is "sudo", etc.

**Then Implement:**
- [ ] Add `seed_default_hitl_settings(db)` to `db.py` that uses `INSERT OR IGNORE INTO tool_hitl_settings` for each tool with its default mode per the story spec table.
- [ ] Call `seed_default_hitl_settings(db)` in `server.py` lifespan after `init_db()`.

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/unit/test_hitl_defaults.py -v`

---

### Step 13: Wire All Routers & End-to-End Smoke Test

**Files:**
- Modify: `/mnt/dev/hive-tools/server.py` -- include all tool routers, UI router
- Create: `/mnt/dev/hive-tools/Dockerfile`
- Create: `/mnt/dev/hive-tools/docker-compose.yml`

**Test First (API):** `tests/api/test_e2e.py`
- [ ] `test_full_hitl_flow_off_mode` -- create token, call calendar endpoint (off mode), assert 200 response with data
- [ ] `test_full_hitl_flow_on_mode` -- create token, call gmail send endpoint (on mode), assert 202, insert approval, poll, assert 200 after approval
- [ ] `test_full_hitl_flow_deny` -- create token, call gmail send, assert 202, deny, poll, assert 403
- [ ] `test_full_hitl_flow_timeout` -- create token, call gmail send, assert 202, advance time past expiry, poll, assert 408
- [ ] `test_full_hitl_flow_sudo_mode` -- create token, call browser navigate (sudo mode), assert 202, approve, verify sudo_grant created, call again, assert passes through without HITL
- [ ] `test_all_tool_routes_registered` -- asserts all expected paths exist in `app.routes`

**Then Implement:**
- [ ] In `server.py`, add `app.include_router(calendar_router)`, `app.include_router(gmail_router)`, `app.include_router(linkedin_router)`, `app.include_router(docker_router)`, `app.include_router(ui_router)`.
- [ ] Create `Dockerfile` based on `/mnt/dev/hive_mind_mcp/Dockerfile` pattern: python:3.12-slim, install Docker CLI, copy app code, `CMD ["python", "server.py"]`.
- [ ] Create `docker-compose.yml` based on `/mnt/dev/hive_mind_mcp/docker-compose.yml` pattern: service name `hive-tools`, container name `hive-tools`, port 9421, volumes for credentials and Docker socket, network `hivemind`.

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/ -v`

---

### Step 14: Telegram HITL Notification with Inline Buttons

**Files:**
- Modify: `/mnt/dev/hive-tools/hitl.py` -- implement `notify_daniel()` with Telegram inline keyboard

**Test First (unit):** `tests/unit/test_hitl_notify.py`
- [ ] `test_notify_daniel_sends_telegram_message` -- asserts `notify_daniel()` calls Telegram sendMessage API with correct payload (mocked httpx)
- [ ] `test_notify_daniel_includes_inline_keyboard` -- asserts the Telegram payload includes `reply_markup` with `inline_keyboard` containing approve/deny buttons
- [ ] `test_notify_daniel_button_callback_data_format` -- asserts callback_data is `hitl_approve_{request_id}` and `hitl_deny_{request_id}`
- [ ] `test_notify_daniel_missing_credentials_fails_gracefully` -- asserts no crash when TELEGRAM_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID is missing

**Then Implement:**
- [ ] Implement `notify_daniel(tool_name, caller_mind, params_json, request_id)` in `hitl.py`:
  - Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_CHAT_ID` from environment variables
  - Sends POST to `https://api.telegram.org/bot{token}/sendMessage` with:
    - `text`: formatted message with tool name, caller mind, params summary
    - `reply_markup`: inline keyboard with Approve/Deny buttons (pattern from `specs/hitl-telegram-inline-buttons.md`)
  - Uses `httpx` (sync, since this is called from a FastAPI dependency)
  - Logs warning and continues if notification fails (do not block the 202 response)
- [ ] Add Telegram webhook endpoint `POST /telegram/hitl-callback` to `server.py` for receiving button callbacks (calls `respond_to_hitl()` internally)

**Verify:** `cd /mnt/dev/hive-tools && python -m pytest tests/unit/test_hitl_notify.py -v`

---

## Integration Checklist

- [ ] All tool routers included in `server.py` via `app.include_router()`
- [ ] UI router included in `server.py`
- [ ] Jinja2 templates directory mounted in FastAPI app
- [ ] `init_db()` called in lifespan to create/migrate all tables
- [ ] `seed_default_hitl_settings()` called in lifespan
- [ ] Default admin user created on first startup if users table empty
- [ ] `requirements.txt` includes all dependencies
- [ ] `Dockerfile` includes Docker CLI for docker_ops tools
- [ ] `docker-compose.yml` wires credentials volume, Docker socket, hivemind network
- [ ] `data/` directory created for SQLite persistence (Docker volume)
- [ ] Session cookies set with `httponly=True, samesite="lax", secure=True`
- [ ] Raw tokens shown exactly once at creation, stored only as SHA-256 hashes
- [ ] HITL requests expire after 5 minutes (300 seconds)
- [ ] Sudo grants expire after configured timeout (default 15 minutes)
- [ ] Telegram notification includes inline keyboard buttons for approve/deny

## Build Verification

- [ ] `cd /mnt/dev/hive-tools && python -m pytest tests/ -v` passes
- [ ] `cd /mnt/dev/hive-tools && python -m mypy . --ignore-missing-imports` passes
- [ ] `cd /mnt/dev/hive-tools && python -m ruff check .` passes
- [ ] All ACs from story addressed:
  - [ ] FastAPI skeleton with bearer token auth
  - [ ] Database with five tables initialized on startup
  - [ ] Token generation + SHA-256 hash storage
  - [ ] `require_api_token()` validates bearer tokens
  - [ ] Auth UI with `ht_session` cookie
  - [ ] Login endpoint functional
  - [ ] `hitl_gate()` with off/on/sudo modes
  - [ ] HITL approval flow end-to-end
  - [ ] Management UI pages (dashboard, tools, tokens, hitl)
  - [ ] All tools migrated and functional
  - [ ] Structured JSON responses
