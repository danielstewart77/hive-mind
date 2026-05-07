# Group Sessions Gateway Endpoints

## User Requirements

The gateway needs three new endpoints to support group chat sessions where multiple minds respond to a single user message. Clients (Telegram HiveMind bot) use these endpoints exclusively — child sessions are managed internally and never addressed directly by clients.

## User Acceptance Criteria

- [ ] `POST /group-sessions` creates a group session and returns a session id
- [ ] `POST /group-sessions` accepts optional `moderator_mind_id` (defaults to `ada`)
- [ ] `GET /group-sessions/{id}` returns session metadata and full time-ordered unified transcript with mind attribution
- [ ] `POST /group-sessions/{id}/message` routes the message to the moderator's child session only
- [ ] `POST /group-sessions/{id}/message` returns an SSE stream of all mind responses as they complete
- [ ] Each SSE event includes `mind_id` attribution
- [ ] Existing `/sessions` endpoints are unchanged
- [ ] Child sessions are not addressable by clients directly

## Technical Specification

### New SQLite tables

```sql
CREATE TABLE IF NOT EXISTS group_sessions (
    id TEXT PRIMARY KEY,
    moderator_mind_id TEXT NOT NULL DEFAULT 'ada',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP
);

-- sessions table gains one new column:
ALTER TABLE sessions ADD COLUMN group_session_id TEXT REFERENCES group_sessions(id);
```

### Endpoints

#### POST /group-sessions
- Body: `{ "moderator_mind_id": "ada" }` (optional)
- Action: Insert into `group_sessions`, spawn a child session for the moderator via existing `_spawn()` logic
- Returns: `{ "id": "<group_session_id>", "moderator_mind_id": "ada" }`

#### GET /group-sessions/{id}
- Returns: group session metadata + all messages from child sessions tagged with this `group_session_id`, ordered by timestamp

#### POST /group-sessions/{id}/message
- Body: `{ "content": "..." }`
- Routes ONLY to the moderator's child session (Ada runs `/moderate` skill)
- Streams SSE events as child minds respond
- Each event shape: `{ "mind_id": "nagatha", "type": "assistant", "message": {...} }`

### Architecture

The gateway routes to the moderator only. The moderator fans out via `forward_to_mind` tool (Phase 4a). The gateway never performs independent fan-out.

## Code References

- `server.py` — add three new route handlers
- `core/sessions.py` — add group session DB methods (`create_group_session`, `get_group_session`, `get_group_transcript`)
- `data/sessions.db` — schema migration (new table + column)

## Implementation Order

1. Add `group_sessions` table and `group_session_id` column migration to `sessions.py`
2. Add `create_group_session()` / `get_group_session()` / `get_group_transcript()` to `SessionManager`
3. Add `POST /group-sessions` route to `server.py`
4. Add `GET /group-sessions/{id}` route
5. Add `POST /group-sessions/{id}/message` route (SSE, routes to moderator child session)
