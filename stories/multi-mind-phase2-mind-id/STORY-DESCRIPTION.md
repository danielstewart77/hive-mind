# [Multi-Mind] Phase 2 — mind_id flows through gateway + session manager

**Card ID:** 1735358468149217106

## Description

Wire mind_id through the stack so the session manager can spawn the right subprocess per mind. Backward compatible — defaults to 'ada' everywhere.

## Scope

- `server.py`: add optional `mind_id` param (default: 'ada') to POST /sessions
- `core/sessions.py`: read mind config from config.yaml minds block, use soul path + backend from config at spawn
- `data/sessions.db`: add `mind_id` column to sessions table (migration)
- Existing Ada bot unchanged — it passes mind_id='ada' explicitly or relies on default

## Acceptance Criteria

- POST /sessions with no mind_id behaves identically to today
- POST /sessions with mind_id='ada' spawns Ada's subprocess with souls/ada.md
- sessions table has mind_id column populated on new sessions
- No existing functionality broken

## Dependencies

- Phase 1 card must be complete first (souls/ directory + config block)

## Reference

Spec: `specs/multi-mind.md` § Phase 2
