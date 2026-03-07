# Story State Tracker

Story: [Memory] Monthly Review Pass — World-Events, Intentions, Session-Logs
Card: 1723686142072587807
Branch: story/memory-monthly-review

## Progress

- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][X] Implement with TDD
- [state 4][X] Code review
- [state 5][ ] Ready for merge

## Acceptance Criteria

- [ ] Scheduler job runs monthly and queries all entries with `data_class` in (`world-event`, `intention`, `session-log`) that haven't been reviewed in the past 30 days
- [ ] Batches entries by class and sends Daniel a Telegram review message
- [ ] Review message format is grouped by class (world-events together, intentions together, session-logs together)
- [ ] Each entry includes brief summary + date stored + options (keep / archive / discard)
- [ ] Single batched message or thread per class group (not one message per entry)
- [ ] Keep response sets `last_reviewed_at = now`, no other change
- [ ] Archive response (world-event only) moves entry to long-term document store, removes from active vector store, marks `archived=True`
- [ ] Discard response deletes entry from vector store and/or graph
- [ ] Long-term archive store for world-events is implemented (placeholder using JSON file in `/data/` with design for later migration)
- [ ] `memory_retrieve` excludes archived entries by default and supports `include_archived=True` flag
- [ ] Tests verify monthly job correctly identifies entries due for review
- [ ] Tests verify Keep updates `last_reviewed_at`
- [ ] Tests verify Archive moves entry to archive store and removes from active retrieval
- [ ] Tests verify Discard removes entry entirely
- [ ] Tests verify `memory_retrieve` excludes archived entries by default
