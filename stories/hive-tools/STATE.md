# Story State Tracker

Story: Build HiveTools — MCP to FastAPI REST migration
Card: 1760363029599356212
Branch: story/hive-tools

## Progress

- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][X] Implement with TDD
- [state 4][X] Code review
- [state 5][ ] Ready for merge

## Acceptance Criteria

- [ ] FastAPI app skeleton created with bearer token auth dependency
- [ ] Database initialized with all five tables (users, api_tokens, tool_hitl_settings, hitl_requests, sudo_grants)
- [ ] Token generation (secrets.token_urlsafe(32)) and SHA-256 hash storage working
- [ ] require_api_token() dependency validates bearer tokens and returns caller mind name
- [ ] Spark to Bloom auth.py copied with hive-tools modifications (cookie name, salt, env vars)
- [ ] Login endpoint (POST /login) functional with session cookie
- [ ] Login UI page (GET /login) renders
- [ ] Session dependency parses and validates ht_session cookie
- [ ] hitl_gate(tool_name) dependency implemented with mode logic (off/on/sudo)
- [ ] HITL request creation and notification to Daniel working
- [ ] Polling endpoint (GET /hitl/{request_id}) returns correct status codes (200/202/403/408)
- [ ] HITL response handler (POST /hitl/{request_id}/respond) updates requests and creates sudo grants
- [ ] Management UI dashboard (GET /) displays HITL activity and token stats
- [ ] Tool inventory page (GET /tools) auto-discovers endpoints and shows HITL mode selectors
- [ ] Token management page (GET /tokens) allows create/revoke operations
- [ ] HITL approval page (GET /hitl) lists pending requests with approve/deny buttons
- [ ] Calendar tool migrated to GET /calendar/events with HITL gate (off mode)
- [ ] Gmail tools migrated: GET /gmail/messages (off), POST /gmail/send (on)
- [ ] LinkedIn tool migrated to POST /linkedin/post (on mode)
- [ ] Docker ops tool migrated to POST /docker/ops (on mode)
- [ ] Browser tools migrated with sudo HITL mode (if available)
- [ ] All tools preserve original logic and functionality
- [ ] All endpoints return structured JSON responses
- [ ] All sensitive operations properly gated by HITL
- [ ] End-to-end approval flow working: create → notify → poll → approve/deny

## HITL Mode Settings (Defaults)

| Tool | Endpoint | Default Mode |
|---|---|---|
| calendar | GET /calendar/events | off |
| gmail-read | GET /gmail/messages | off |
| gmail-send | POST /gmail/send | on |
| linkedin | POST /linkedin/post | on |
| docker | POST /docker/ops | on |
| browser-navigate | POST /browser/navigate | sudo |
| browser-click | POST /browser/click | sudo |
| browser-read | POST /browser/read | sudo |
| browser-submit | POST /browser/submit | on |

## Database Tables

- users: Management UI authentication
- api_tokens: Mind API access tokens (SHA-256 hashes)
- tool_hitl_settings: Per-tool HITL mode and timeout configuration
- hitl_requests: Pending/approved/denied approval requests with expiry
- sudo_grants: Time-limited auto-approval windows after first approval

## Key Implementation Notes

- All bearer tokens stored as SHA-256 hashes only
- Raw token shown once at generation time, never retrievable again
- Session cookies httponly, samesite=lax, secure=True
- HITL request expires after 5 minutes if pending
- Sudo grants auto-expire after configured timeout (default 15 minutes)
- Tool inventory auto-discovered from FastAPI app.routes
- No manual tool registration needed for management UI
