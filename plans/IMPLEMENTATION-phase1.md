# Implementation Plan: Mind-to-Mind Messaging Broker (Phase 1)

## Overview

Add asynchronous mind-to-mind messaging to the Hive Mind gateway. A broker module (`core/broker.py`) manages conversations and messages in a separate SQLite database. When a mind sends a message, the broker writes it to the DB, kicks off a background task that wakes the callee via the session manager, collects the response from the SSE stream, and writes it back. A polling script (`tools/stateless/poll_broker/poll_broker.py`) and Haiku agent (`poll-task-result`) deliver the result to the calling mind's thread.

## Technical Approach

- **Integrated broker** — new `/broker/*` endpoints in `server.py`, backed by `core/broker.py`. No new container.
- **Separate SQLite** — `data/broker.db` with `conversations` and `messages` tables. Separate from `sessions.db`.
- **Background wakeup** — `asyncio.create_task()` in the POST handler. The task creates a callee session via `session_mgr.create_session()`, sends the wakeup prompt via `session_mgr.send_message()`, and collects the full response by iterating the async generator (same pattern as `delegate_to_mind` in `tools/stateful/inter_mind.py`).
- **Collection backstop** — generous timeout per `request_type` (8x notification threshold). Tasks can legitimately run for hours. The backstop is a safety net, not a policy tool.
- **Callee is unaware** — the callee responds normally through its session. No POST-back. The broker's background task captures the response.
- **Polling agent = Haiku wrapper around a Python script** — the script does `time.sleep(30)` + `requests.get()` in a loop (zero tokens). The agent only consumes tokens at start and on result delivery.
- **Idempotent POST** — caller provides `message_id`. Duplicate POSTs return existing status, no duplicate rows.
- **Restart recovery** — on startup, re-dispatch `pending` messages, fail `dispatched` messages (callee session is dead after restart).
- **TDD** — every step writes tests first, then implementation.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|------------|-------|
| SSE response collection | `tools/stateful/inter_mind.py:61-86` | Background task collects callee response from async generator |
| Session creation for delegation | `tools/stateful/inter_mind.py:47-58` | `create_session(owner_type="broker", ...)` |
| API test with mocked session_mgr | `tests/api/test_session_mind_id.py` | `patch("server.session_mgr")` + `TestClient` |
| API test with mocked session_mgr | `tests/api/test_group_sessions.py` | Same pattern for group session endpoints |
| Unit test with mocked requests | `tests/unit/test_delegate_to_mind.py` | Mock HTTP responses, test JSON output |
| Stateless tool with SQLite + argparse | `tools/stateless/reminders/reminders.py` | Standalone script, `argparse`, `json.dumps` to stdout |
| Agent definition frontmatter | `.claude/agents/parse-memory.md` | `name`, `description`, `tools`, `model`, `maxTurns` |
| SQLite with aiosqlite | `core/sessions.py:234-274` | `aiosqlite.connect()`, `executescript()`, schema migration |
| Logging conventions | `specs/logging.md` | `log.info("broker: action key=value")` format |

## Models & Schemas

### Pydantic models (in `server.py`)

```python
class BrokerMessageRequest(BaseModel):
    message_id: str | None = None  # caller-provided for idempotency; auto-generated if omitted
    conversation_id: str
    from_mind: str = Field(alias="from")
    to_mind: str = Field(alias="to")
    content: str
    rolling_summary: str = ""
    metadata: dict | None = None

    model_config = ConfigDict(populate_by_name=True)

class BrokerMessageResponse(BaseModel):
    status: str  # "dispatched" or "exists"
    conversation_id: str
    message_id: str
```

**Note on field naming:** The spec uses `from`/`to` in JSON payloads, but Python reserves `from`. Use `from_mind`/`to_mind` as Python field names with Pydantic `Field(alias="from")` / `Field(alias="to")` so the JSON interface matches the spec while the Python code is clean. `populate_by_name=True` allows both `"from"` and `"from_mind"` in incoming JSON.

### SQLite schema (in `core/broker.py`)

```sql
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                    TEXT PRIMARY KEY,
    conversation_id       TEXT NOT NULL REFERENCES conversations(id),
    from_mind             TEXT NOT NULL,
    to_mind               TEXT NOT NULL,
    message_number        INTEGER NOT NULL,
    content               TEXT NOT NULL,
    rolling_summary       TEXT DEFAULT '',
    metadata              TEXT,
    status                TEXT NOT NULL DEFAULT 'pending',
    recipient_session_id  TEXT,
    response_error        TEXT,
    timestamp             REAL NOT NULL,
    UNIQUE(conversation_id, message_number)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
```

### Status lifecycle

| Status | Meaning |
|--------|---------|
| `pending` | Message written to DB, background wakeup task not yet started |
| `dispatched` | Callee session created, wakeup sent, awaiting response |
| `completed` | Callee response collected and written as the next message |
| `failed` | Wakeup or response collection failed (detail in `response_error`) |
| `timed_out` | Collection backstop exceeded (detail in `response_error`) |

### Collection backstop per request_type

| `request_type` | Backstop (8x notification threshold) |
|----------------|--------------------------------------|
| `quick_query` | 40 min |
| `research` | 160 min |
| `code_review` | 160 min |
| `content_generation` | 120 min |
| `data_analysis` | 160 min |
| `security_triage` | 240 min |
| `security_remediation` | 720 min |
| (unknown/missing) | 360 min |

---

## Implementation Steps

### Step 1: Broker data layer — `core/broker.py`

Pure data operations against SQLite. No FastAPI dependency. No session manager dependency.

**Files:**
- Create: `core/broker.py` — SQLite connection, table creation, CRUD functions, restart recovery

**Test First (unit):** `tests/unit/test_broker.py`
- [ ] `test_init_db_creates_tables` — call `init_db()` with a temp file, verify both tables exist via `sqlite3`
- [ ] `test_init_db_creates_indexes` — verify `idx_messages_conversation_id` and `idx_messages_status` exist
- [ ] `test_create_conversation_inserts_row` — call `create_conversation(id)`, query DB, verify row
- [ ] `test_insert_message_inserts_row` — call `insert_message(...)`, query DB, verify all fields including `status`, `response_error`, `recipient_session_id`
- [ ] `test_insert_message_auto_creates_conversation` — insert a message for a conversation that doesn't exist yet; verify conversation row is created
- [ ] `test_insert_message_duplicate_id_is_idempotent` — insert a message, insert again with same `id`, verify only one row exists and the second call returns the existing status
- [ ] `test_insert_message_duplicate_message_number_rejected` — insert two messages with the same `(conversation_id, message_number)`, verify UNIQUE constraint raises
- [ ] `test_get_messages_returns_ordered_by_message_number` — insert 3 messages out of order, call `get_messages(conversation_id)`, verify ordered
- [ ] `test_get_messages_filters_by_conversation_id` — insert messages for two conversations, verify only the requested one is returned
- [ ] `test_update_message_status` — insert a message, call `update_message_status(id, "dispatched")`, verify
- [ ] `test_update_message_status_with_error` — call `update_message_status(id, "failed", response_error="timeout")`, verify both fields
- [ ] `test_get_next_message_number_starts_at_1` — empty conversation, verify returns 1
- [ ] `test_get_next_message_number_increments` — insert messages 1 and 2, verify returns 3
- [ ] `test_recover_stranded_pending_returns_messages` — insert a `pending` message, call `get_stranded_messages()`, verify it appears in the `pending` list
- [ ] `test_recover_stranded_dispatched_marks_failed` — insert a `dispatched` message, call `recover_stranded_messages(db)`, verify it becomes `status='failed'` with `response_error` containing `server_restart`

**Then Implement:**
- [ ] Create `core/broker.py` with:
  - `_SCHEMA` constant with full DDL (tables, constraints, indexes) matching the spec exactly
  - `async def init_db(db_path: str) -> aiosqlite.Connection` — connect, create tables, return connection. Follow the pattern in `core/sessions.py:234-242`.
  - `async def create_conversation(db, conversation_id: str)` — INSERT into conversations
  - `async def insert_message(db, *, message_id, conversation_id, from_mind, to_mind, message_number, content, rolling_summary, metadata, status) -> dict` — INSERT OR IGNORE into messages (idempotent on `id`). If row already exists, return existing row as dict. Auto-create conversation row via INSERT OR IGNORE.
  - `async def get_messages(db, conversation_id: str) -> list[dict]` — SELECT * FROM messages WHERE conversation_id=? ORDER BY message_number
  - `async def get_message(db, message_id: str) -> dict | None` — SELECT * FROM messages WHERE id=?
  - `async def update_message_status(db, message_id: str, status: str, *, recipient_session_id=None, response_error=None)` — UPDATE messages SET status=?, recipient_session_id=?, response_error=? WHERE id=?
  - `async def get_next_message_number(db, conversation_id: str) -> int` — SELECT COALESCE(MAX(message_number), 0) + 1
  - `async def get_stranded_messages(db) -> dict` — returns `{"pending": [...], "dispatched": [...]}` — messages with non-terminal status
  - `async def recover_stranded_messages(db)` — mark `dispatched` → `failed` with `response_error='server_restart_during_delivery'`. Return `pending` list for re-dispatch by the caller.
  - `BACKSTOP_SECONDS` dict mapping `request_type` to seconds (from spec table)
  - `def get_backstop_seconds(request_type: str | None) -> int` — lookup with 21600 (6 hr) default

**Verify:** `pytest tests/unit/test_broker.py -v`

---

### Step 2: Wakeup and response collection — `core/broker.py`

The async function that creates a callee session, sends the wakeup prompt, and collects the full response from the SSE stream. Includes collection backstop timeout.

**Files:**
- Modify: `core/broker.py` — add `wakeup_and_collect()` and `build_wakeup_prompt()` functions

**Test First (unit):** `tests/unit/test_broker_wakeup.py`
- [ ] `test_build_wakeup_prompt_first_message` — `message_number=1`, empty `rolling_summary`. Verify prompt contains `from_mind`, `conversation_id`, `content`. Verify no "Summary of conversation" block.
- [ ] `test_build_wakeup_prompt_followup_message` — `message_number=2`, non-empty `rolling_summary`. Verify prompt includes the summary block.
- [ ] `test_wakeup_creates_session_and_sends_message` — mock `session_mgr.create_session` (returns `{"id": "sess-1", ...}`) and `session_mgr.send_message` (async generator yielding `[{"type": "assistant", "message": {"content": [{"type": "text", "text": "response"}]}}, {"type": "result", "result": "response"}]`). Verify session created with `owner_type="broker"` and `mind_id=to_mind`. Verify wakeup prompt sent. Verify response text collected.
- [ ] `test_wakeup_writes_response_as_new_message` — verify that after collection, a new message row exists with `from_mind=callee`, `to_mind=caller`, `status="completed"`, and the response content
- [ ] `test_wakeup_transitions_status_pending_to_dispatched_to_completed` — verify the original message passes through `pending` → `dispatched` → `completed`
- [ ] `test_wakeup_kills_callee_session_after_collection` — verify `session_mgr.kill_session(session_id)` is called after response is collected
- [ ] `test_wakeup_handles_session_creation_failure` — mock `create_session` to raise `ValueError`. Verify original message updated to `status="failed"` with error detail in `response_error`.
- [ ] `test_wakeup_handles_send_message_exception` — mock `send_message` to raise. Verify original message → `status="failed"`.
- [ ] `test_wakeup_handles_empty_response` — mock `send_message` to yield only a `result` event with empty text. Verify response message is still written (with empty content), status is `completed`.
- [ ] `test_wakeup_backstop_timeout` — mock `send_message` to never yield a `result` event (hang). Use a very short backstop (1 second). Verify original message → `status="timed_out"`. Verify callee session killed.

**Then Implement:**
- [ ] Add `build_wakeup_prompt(from_mind, to_mind, conversation_id, content, rolling_summary, message_number) -> str` — constructs the wakeup prompt per the spec's "Wakeup Prompt" section
- [ ] Add `async def _collect_response(send_generator) -> str` — iterates the async generator, extracts text from `assistant` events (same logic as `inter_mind.py:70-86`), returns assembled response text
- [ ] Add `async def wakeup_and_collect(db, session_mgr, *, message_id, conversation_id, from_mind, to_mind, content, rolling_summary, message_number, metadata)`:
  1. Determine backstop timeout from `metadata.get("request_type")` via `get_backstop_seconds()`
  2. Update message status to `"dispatched"`
  3. Build wakeup prompt via `build_wakeup_prompt()`
  4. Create callee session: `await session_mgr.create_session(owner_type="broker", owner_ref=f"broker-{conversation_id}", client_ref=f"broker-{conversation_id}-{to_mind}", mind_id=to_mind)`
  5. Update message with `recipient_session_id`
  6. Collect response with backstop: `asyncio.wait_for(_collect_response(session_mgr.send_message(...)), timeout=backstop_seconds)`
  7. Write response as new message: `insert_message(db, from_mind=to_mind, to_mind=from_mind, content=response_text, status="completed", message_number=next_number, ...)`
  8. Update original message status to `"completed"`
  9. Kill callee session: `await session_mgr.kill_session(session_id)`
  10. On `asyncio.TimeoutError`: update original message to `status="timed_out"`, `response_error=f"backstop exceeded ({backstop_seconds}s)"`. Kill callee session. Log at WARNING.
  11. On any other exception: update original message to `status="failed"`, `response_error=str(e)`. Kill callee session if it was created. Log at ERROR.

**Verify:** `pytest tests/unit/test_broker_wakeup.py -v`

---

### Step 3: Broker API endpoints — `server.py`

Add `/broker/messages` POST and GET endpoints, plus startup recovery.

**Files:**
- Modify: `server.py` — add Pydantic models, broker init in lifespan, POST/GET endpoints, startup recovery

**Test First (API):** `tests/api/test_broker_endpoints.py`
- [ ] `test_post_broker_message_returns_dispatched` — POST to `/broker/messages` with valid payload, mock `session_mgr`, verify 200 response with `status="dispatched"`, `conversation_id`, `message_id`
- [ ] `test_post_broker_message_missing_required_fields` — POST without `conversation_id`, verify 422
- [ ] `test_post_broker_message_unknown_mind_returns_404` — POST with `to_mind` that has no `minds/<name>/implementation.py`, verify 404 with clear error
- [ ] `test_post_broker_message_idempotent_on_duplicate_id` — POST a message with `message_id="msg-1"`, POST again with same `message_id`, verify second response has `status="exists"` and no duplicate row
- [ ] `test_post_broker_message_accepts_from_alias` — POST with `"from": "ada"` (JSON alias), verify `from_mind` is correctly parsed
- [ ] `test_get_broker_messages_returns_messages_for_conversation` — insert test data via POST, GET `/broker/messages?conversation_id=<id>`, verify JSON array with correct messages
- [ ] `test_get_broker_messages_empty_conversation_returns_empty_list` — GET with unknown conversation_id, verify empty array
- [ ] `test_get_broker_conversation_returns_detail` — GET `/broker/conversations/<id>`, verify conversation with all messages
- [ ] `test_get_broker_conversation_not_found_returns_404` — GET with unknown id, verify 404

**Then Implement:**
- [ ] Add Pydantic models to `server.py`: `BrokerMessageRequest` (with `Field(alias="from")` / `Field(alias="to")`, `populate_by_name=True`), `BrokerMessageResponse`
- [ ] In `lifespan()` startup:
  1. Initialize broker DB via `broker.init_db(broker_db_path)`, store connection as `broker_db`
  2. Run `broker.recover_stranded_messages(broker_db)` — marks dispatched → failed
  3. For each returned pending message: kick off `asyncio.create_task(broker.wakeup_and_collect(...))`
  4. Log recovery summary: `log.info("broker: startup recovery pending=%d dispatched_failed=%d", ...)`
- [ ] Add `POST /broker/messages` endpoint:
  1. Validate `to_mind` via filesystem: check `minds/<to_mind>/implementation.py` exists — return 404 if not. No dependency on `config.yaml`.
  2. Use caller-provided `message_id` or generate UUID
  3. Compute `message_number` via `get_next_message_number()`
  4. Call `insert_message()` — if row already existed (idempotent), return `BrokerMessageResponse(status="exists", ...)`
  5. Kick off `asyncio.create_task(broker.wakeup_and_collect(broker_db, session_mgr, ...))`
  6. Return `BrokerMessageResponse(status="dispatched", conversation_id=..., message_id=...)`
- [ ] Add `GET /broker/messages` endpoint: accept `conversation_id` query param, call `get_messages()`, return JSON array
- [ ] Add `GET /broker/conversations/{conversation_id}` endpoint: call `get_messages()`, return 404 if empty or conversation doesn't exist
- [ ] In `lifespan()` shutdown: close broker DB connection

**Verify:** `pytest tests/api/test_broker_endpoints.py -v`

---

### Step 4: Polling script — `tools/stateless/poll_broker/poll_broker.py`

Pure Python script. No LLM. Polls `GET /broker/messages`, checks for callee response, handles notification threshold.

**Files:**
- Create: `tools/stateless/poll_broker/poll_broker.py` — standalone script with argparse + JSON stdout
- Create: `tools/stateless/poll_broker/__init__.py` — empty

**Test First (unit):** `tests/unit/test_poll_broker.py`
- [ ] `test_parse_args_all_required` — call `parse_args()` with all required args, verify namespace
- [ ] `test_parse_args_gateway_url_default` — omit `--gateway_url`, verify defaults to `http://localhost:8420`
- [ ] `test_threshold_lookup_returns_correct_values` — verify `get_threshold("quick_query")` returns 300, `get_threshold("security_remediation")` returns 5400
- [ ] `test_threshold_lookup_unknown_type_returns_default` — verify fallback to 1200 (20 min)
- [ ] `test_hard_ceiling_is_4x_threshold` — verify `get_hard_ceiling("quick_query")` returns 1200
- [ ] `test_check_for_result_finds_callee_response` — mock `requests.get` to return JSON with messages including one from the callee (`from_mind == to_mind` arg), verify function returns the response dict
- [ ] `test_check_for_result_returns_none_when_no_response` — mock `requests.get` to return only the outgoing message, verify returns None
- [ ] `test_check_for_result_ignores_pending_callee_messages` — mock response includes a callee message with `status="pending"`, verify returns None (only `completed` messages count)
- [ ] `test_build_notification_message` — verify format includes request_type, threshold, conversation_id

**Then Implement:**
- [ ] Create `tools/stateless/poll_broker/poll_broker.py` following the pattern in `tools/stateless/reminders/reminders.py`:
  - `argparse` with `--conversation_id`, `--from_mind`, `--to_mind`, `--request_type`, `--gateway_url` (default `http://localhost:8420`)
  - `THRESHOLDS` dict mapping `request_type` to seconds
  - `get_threshold(request_type) -> int` with 1200s default
  - `get_hard_ceiling(request_type) -> int` (4x threshold)
  - `check_for_result(gateway_url, conversation_id, to_mind) -> dict | None` — GET `/broker/messages?conversation_id=<id>`, check for `completed` message where `from_mind == to_mind`
  - `send_notification(message)` — call `tools/stateless/notify/notify.py` via subprocess, or print to stderr as fallback
  - Main loop:
    1. Poll every 30 seconds
    2. If result found: print JSON `{"status": "completed", "response": "...", "conversation_id": "...", "from_mind": "..."}` to stdout, exit 0
    3. If threshold exceeded: send notification, switch to 3-min intervals (daytime) or 4-hour intervals (night)
    4. If hard ceiling exceeded: print JSON `{"status": "timeout", "conversation_id": "...", "elapsed_seconds": ...}` to stdout, exit 1

**Verify:** `pytest tests/unit/test_poll_broker.py -v`

---

### Step 5: Polling agent — `.claude/agents/poll-task-result.md`

Haiku agent that runs the polling script and delivers the result.

**Files:**
- Create: `.claude/agents/poll-task-result.md` — agent definition

**Test First:** No automated test — agent files are markdown. Verification is manual (agent invocation).

**Then Implement:**
- [ ] Create `.claude/agents/poll-task-result.md` with frontmatter:
  ```yaml
  name: poll-task-result
  description: Polls the broker for an inter-mind task result, delivers it when found.
  tools: Bash, Read
  model: claude-haiku-4-5-20251001
  maxTurns: 5
  ```
- [ ] Body instructs the agent to:
  1. Run the polling script via Bash: `python3 tools/stateless/poll_broker/poll_broker.py --conversation_id <id> --from_mind <from> --to_mind <to> --request_type <type> --gateway_url http://server:8420`
  2. On exit 0: read stdout JSON, deliver the response content as a message
  3. On exit 1: read stdout JSON, deliver a timeout warning

**Verify:** Manual — inspect file, verify frontmatter parses correctly.

---

### Step 6: Send skill — `.claude/skills/send-message-to-mind/SKILL.md`

The skill that minds use to send messages. Handles conversation ID generation, rolling summary, metadata, and polling agent spawn.

**Files:**
- Create: `.claude/skills/send-message-to-mind/SKILL.md` — skill definition

**Test First:** No automated test — skill files are markdown. Verification is manual.

**Then Implement:**
- [ ] Create `.claude/skills/send-message-to-mind/SKILL.md` with the full content from the spec's "Send Skill" section. Frontmatter:
  ```yaml
  name: send-message-to-mind
  description: Send an async message to another mind via the broker.
  user-invocable: false
  ```
- [ ] Adapt the spec content:
  - Step 4 URL: `http://server:8420/broker/messages` (inside container) or `http://localhost:8420/broker/messages` (outside)
  - Step 5: spawn `poll-task-result` agent as a background subagent with the correct parameters
  - Include the `request_type` → threshold table for reference
  - Include the `message_id` field (generate UUID for idempotency)

**Verify:** Manual — inspect file, verify frontmatter and step structure.

---

### Step 7: Integration tests — full broker round-trip and failure modes

End-to-end tests that verify the complete flow and critical failure scenarios.

**Files:**
- Create: `tests/integration/test_broker_round_trip.py`

**Test First (integration):** `tests/integration/test_broker_round_trip.py`
- [ ] `test_broker_round_trip_dispatches_and_collects_response` — Use `TestClient` against `server.app`. Mock `session_mgr.create_session` to return a session dict, mock `session_mgr.send_message` as an async generator yielding assistant text + result event. POST to `/broker/messages`, verify `status="dispatched"`. Wait briefly for background task. GET `/broker/messages?conversation_id=<id>`, verify two messages: the outgoing request (`status="completed"`) and the callee's response (`status="completed"`).
- [ ] `test_broker_round_trip_handles_wakeup_failure` — Mock `session_mgr.create_session` to raise. POST to `/broker/messages`. Wait briefly. GET messages, verify original message has `status="failed"` with `response_error` populated.
- [ ] `test_broker_round_trip_handles_callee_exception` — Mock `session_mgr.send_message` to yield one event then raise an exception. POST to `/broker/messages`. Wait briefly. GET messages, verify original message has `status="failed"`.
- [ ] `test_broker_multi_turn_preserves_conversation` — POST two messages to the same `conversation_id` (simulating turn 1 and turn 3), verify `message_number` increments correctly and all messages appear in GET.
- [ ] `test_broker_idempotent_post_no_duplicate` — POST a message with explicit `message_id`, POST again with same `message_id`, verify only one outgoing message row in GET.
- [ ] `test_broker_startup_recovery_redispatches_pending` — Directly insert a `pending` message into the broker DB. Simulate startup recovery by calling `recover_stranded_messages()` + re-dispatch loop. Verify the message transitions to `dispatched` → `completed`.
- [ ] `test_broker_startup_recovery_fails_dispatched` — Directly insert a `dispatched` message into the broker DB. Call `recover_stranded_messages()`. Verify it becomes `status="failed"` with `response_error` containing `server_restart`.

**Verify:** `pytest tests/integration/test_broker_round_trip.py -v`

---

## Integration Checklist

- [ ] Routes registered in `server.py` under `/broker/*` prefix
- [ ] Broker DB initialized in `lifespan()` startup, closed in shutdown
- [ ] Startup recovery runs in `lifespan()` after broker DB init
- [ ] `core/broker.py` uses `aiosqlite` (same as `core/sessions.py`)
- [ ] SQLite schema matches spec exactly: tables, constraints, indexes
- [ ] Polling script in `tools/stateless/poll_broker/` follows stateless tool conventions
- [ ] Agent file in `.claude/agents/poll-task-result.md` follows existing agent frontmatter format
- [ ] Skill file in `.claude/skills/send-message-to-mind/SKILL.md` follows existing skill format
- [ ] Logging follows `specs/logging.md`: `log.info("broker: action key=value")` format
- [ ] No new dependencies needed — `aiosqlite`, `requests`, `fastapi` already in `requirements.txt`
- [ ] Mind existence validated via filesystem (`minds/<name>/implementation.py`), not `config.yaml` — no cross-dependency
- [ ] No auth on broker endpoints — internal-only, same trust model as existing `delegate_to_mind`
- [ ] Idempotent POST via caller-provided `message_id`
- [ ] Collection backstop derived from `request_type` metadata

## Build Verification

- [ ] `pytest -v` passes (all existing + new tests)
- [ ] `mypy . --ignore-missing-imports` passes
- [ ] `ruff check .` passes
- [ ] All Phase 1 scope items addressed:
  - [ ] Broker endpoints integrated into `server.py`
  - [ ] SQLite storage in `data/broker.db`
  - [ ] Background wakeup + response collection with backstop
  - [ ] Restart recovery for stranded messages
  - [ ] Idempotent POST
  - [ ] `send-message-to-mind` skill
  - [ ] `poll-task-result` agent + polling script
  - [ ] Notification on threshold exceeded
