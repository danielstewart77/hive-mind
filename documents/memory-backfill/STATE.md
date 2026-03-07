# Story State Tracker

Story: [Memory] Existing Data Backfill — Classify All Entries
Card: 1723685647471871507
Branch: story/memory-backfill

## Progress

- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][ ] Implement with TDD
- [state 4][X] Code review
- [state 5][ ] Ready for merge

## Acceptance Criteria

- [ ] Backfill script/tool scans all entries missing `data_class`
- [ ] For each entry, classification attempted against 7 defined classes using content + tags
- [ ] High-confidence matches: auto-assign `data_class`, set `tier`, `as_of` (use `created_at` if available), `source`
- [ ] Low-confidence or no-match entries queued for Daniel review
- [ ] Telegram batch review flow implemented (grouped messages, not one per message)
- [ ] For each unclassified entry: summary and candidate classes shown to Daniel
- [ ] Daniel classification responses applied immediately to entries
- [ ] New classes discovered during backfill added to registry in `memory.py` and spec
- [ ] All existing entries have a `data_class` assigned
- [ ] No unclassified entries remain
- [ ] Any new classes discovered documented in `specs/memory-lifecycle.md`
- [ ] `data_class` field made required (not optional) in `memory_store` and `graph_upsert`
