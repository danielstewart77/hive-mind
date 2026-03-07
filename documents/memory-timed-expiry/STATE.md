# Story State Tracker

Story: [Memory] Timed-Event Auto-Expiry — Nightly Pass
Card: 1723685753000560150
Branch: story/memory-timed-expiry

## Progress
- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][X] Implement with TDD
- [state 4][X] Code review
- [state 5][ ] Ready for merge

## Acceptance Criteria

- [ ] `memory_store` rejects `timed-event` entries without a resolved absolute `expires_at` datetime
- [ ] Invalid time references in content trigger an error prompting reclassification or discard
- [ ] `recurring` boolean flag is set on timed-event entries (default `False`)
- [ ] Nightly scheduler job queries all `timed-event` entries where `expires_at < now`
- [ ] Non-recurring entries are deleted unconditionally
- [ ] Recurring entries trigger a Telegram prompt to Daniel asking to delete or keep for next occurrence
- [ ] All deletions are logged
- [ ] Recurring events detected via keyword heuristics (birthday, anniversary, weekly, monthly, annual, every, recurring)
- [ ] Manual override via explicit `recurring=True` at write time is supported
- [ ] Unit tests cover nightly job deletion of expired non-recurring events
- [ ] Unit tests cover nightly job Telegram prompt for expired recurring events
- [ ] Unit tests verify `memory_store` rejection of entries with unresolved time references

## Dependencies

- Schema & Metadata story (must be completed first)
