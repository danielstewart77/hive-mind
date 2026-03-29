# Remote Control Integration for Hive Mind Sessions

## Overview

Enable Daniel to observe and optionally drive a running Ada (or other mind) session in real-time from his phone or any browser via Claude's Remote Control feature (`claude --remote-control`).

## User Requirements

Daniel wants to be able to:
1. Start watching Ada work in real-time — tool calls, responses, everything — from his phone or browser without being at the terminal
2. Initiate this from the Telegram/group chat interface (e.g. a command like `/rc` or `/remote-control`)
3. Get a session URL or QR code back that he can open in the Claude mobile app or claude.ai/code

## User Acceptance Criteria

- [ ] Spike confirms whether `claude --remote-control --resume <claude_sid>` produces a valid session URL
- [ ] If spike succeeds: sending a `/rc` or remote-control command through the chat interface triggers RC for Ada's current session
- [ ] A usable session URL is returned in the chat response
- [ ] Daniel can open that URL on his phone and see the live session (tool calls, messages, responses)
- [ ] The RC subprocess does not interfere with normal gateway operation — Ada continues responding through the gateway as usual
- [ ] If `--resume` + `--remote-control` is not supported: document the finding and propose Option B (RC-native session mode) as follow-up

## Technical Specification

### Architecture

This is Option A: a "shadow" RC process runs alongside the existing gateway session.

```
Gateway session (claude -p --stream-json)  ←— normal operation
       ↓ same claude_sid
RC subprocess (claude --remote-control --resume <claude_sid> --name "Ada")
       ↓ outbound HTTPS to Anthropic relay
Claude mobile app / claude.ai/code  ←— Daniel observes here
```

### Spike (Phase 1)

Test the flag combination on the host:
```bash
claude --remote-control --resume <claude_sid> --name "Ada"
```

Capture stdout and look for:
- A session URL (format: `https://claude.ai/code/sessions/...`)
- A QR code toggle (spacebar)
- Any errors about incompatible flags

If the flags compose cleanly, proceed to Phase 2. If not, document the incompatibility and propose Option B.

### Phase 2 — Gateway Integration

1. **New endpoint**: `POST /sessions/{id}/remote-control`
   - Reads `claude_sid` from the session record in SQLite
   - Spawns `claude --remote-control --resume <claude_sid> --name "<mind_id>"` as a subprocess
   - Parses stdout for the session URL (regex match on `https://claude.ai/code/...`)
   - Returns `{"url": "...", "session_id": "..."}` to caller

2. **RC process lifecycle**:
   - RC subprocess is tracked alongside the gateway session
   - Killed when the gateway session is killed
   - Restarted if it exits unexpectedly (optional — Phase 3)

3. **Chat trigger** (optional, Phase 3):
   - Add `/rc` skill that calls the new endpoint and relays the URL back to Daniel in chat

### Authentication requirement

Remote Control requires claude.ai OAuth login, not API key. Our sessions already use the OAuth token (confirmed — this is why Bilby uses the same token as Ada). No auth changes needed.

## Code References

| File | Change |
|------|--------|
| `server.py` | New `POST /sessions/{id}/remote-control` endpoint |
| `core/sessions.py` | `spawn_rc_process()` helper + RC process tracking |
| `.claude/skills/rc/SKILL.md` | New skill to trigger RC from chat (Phase 3) |

## Implementation Order

1. **Spike** — test `claude --remote-control --resume <claude_sid>` on the host machine, confirm flag compatibility and URL output format. **Status: manual verification required** -- run `claude --remote-control --resume <claude_sid>` on the host to confirm the flags compose cleanly and a session URL is output to stdout.
2. **Endpoint** — `POST /sessions/{id}/remote-control` in `server.py` **[IMPLEMENTED]**
3. **Delete endpoint** — `DELETE /sessions/{id}/remote-control` in `server.py` **[IMPLEMENTED]**
4. **Process helper** — `spawn_rc_process()` + `kill_rc_process()` in `core/sessions.py`, parse URL from stdout **[IMPLEMENTED]**
5. **Lifecycle** — kill RC subprocess when gateway session ends (integrated into `_kill_process()`, `shutdown()`) **[IMPLEMENTED]**
6. **Skill** (optional) — `/rc` chat trigger that relays the URL back to Daniel
