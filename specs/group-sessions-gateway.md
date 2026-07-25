# Group Sessions Gateway Endpoints

## Current state — DB layer built, HTTP routes missing

The session manager (`nervous-system/comms/sessions.py`) has the full group-session data layer implemented: a `group_sessions` table, a `group_session_id` column on `sessions`, and working methods —

- `create_group_session(moderator_mind_id)`
- `get_group_session(group_session_id)`
- `delete_group_session(group_session_id)`
- `get_or_create_group_child_session(group_session_id, mind_id, surface_prompt=None)`
- `get_group_transcript(group_session_id)` — all child sessions' messages, time-ordered

None of this is exposed over HTTP. `nervous-system/comms/server.py` has no `/group-sessions*` routes at all. `bots/hivemind_bot.py` (the group-chat Telegram bot) already calls `POST /group-sessions` and `POST /group-sessions/{id}/message` as if they existed — those calls 404 today. `config.yaml`'s `group_chat` block (`default_moderator`, `available_minds`) is parsed but has no working end-to-end path to actually drive.

## What's left — the route layer only

### POST /group-sessions
- Body: `{ "moderator_mind_id": "<mind>" }` (optional, falls back to `config.yaml`'s `group_chat.default_moderator`)
- Action: `create_group_session()`, then `get_or_create_group_child_session()` to spawn the moderator's child session
- Returns: `{ "id": "<group_session_id>", "moderator_mind_id": "..." }`

### GET /group-sessions/{id}
- Returns: `get_group_session()` metadata + `get_group_transcript()`, ordered by `last_active`

### POST /group-sessions/{id}/message
- Body: `{ "content": "..." }`
- Routes to the moderator's child session only — the moderator fans out to other minds itself (its own `/moderate` skill), the gateway does not fan out independently
- Streams SSE events tagged with `mind_id` as child minds respond

### DELETE /group-sessions/{id}
- Calls `delete_group_session()` — not in the original plan, but the manager method already exists, so add the route for symmetry

## Code References

- `nervous-system/comms/server.py` — add four new route handlers
- `nervous-system/comms/sessions.py` — DB layer already implemented, no changes needed
- `bots/hivemind_bot.py` — already assumes these routes exist; verify its call shapes match once the routes land

## Implementation Order

1. Add `POST /group-sessions`, `GET /group-sessions/{id}`, `POST /group-sessions/{id}/message`, `DELETE /group-sessions/{id}` to `nervous-system/comms/server.py`, calling straight into the existing `SessionManager` methods
2. Verify `bots/hivemind_bot.py`'s existing calls actually match the new routes' request/response shapes
3. Confirm `config.yaml`'s `group_chat.default_moderator`/`available_minds` are read somewhere in the new route logic — currently parsed by `config.py` but unused by anything
