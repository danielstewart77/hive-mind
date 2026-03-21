# Story State Tracker

Story: [Multi-Mind] Phase 2 — mind_id flows through gateway + session manager
Card: 1735358468149217106
Branch: story/multi-mind-phase2-mind-id

## Progress

- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][ ] Implement with TDD
- [state 4][X] Code review
- [state 5][ ] Ready for merge

## Acceptance Criteria

- [ ] POST /sessions with no mind_id behaves identically to today
- [ ] POST /sessions with mind_id='ada' spawns Ada's subprocess with souls/ada.md
- [ ] sessions table has mind_id column populated on new sessions
- [ ] No existing functionality broken
