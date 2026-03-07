# [Memory] Existing Data Backfill — Classify All Entries

**Card ID:** 1723685647471871507

## Description

One-time migration to classify all existing memory entries and graph nodes using the data class definitions in `specs/memory-lifecycle.md`. Runs after the Schema & Metadata story is complete.

### Approach

Iterate all existing entries that lack a `data_class` field. For each, attempt to classify using the known class definitions. If no class fits, send Daniel a Telegram message describing the entry and asking how to classify it. Daniel's response defines or maps to a class, which is immediately applied and added to the registry if new.

This process is also the primary mechanism for discovering missing data classes — after enough entries are reviewed, the registry will cover the real distribution of data in the system.

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

## Tasks

### Backfill Script / Tool
- Create one-time runnable tool (or scheduler job flag) that scans all entries missing `data_class`
- Implement classification logic against 7 defined classes using content + tags
- Auto-assign high-confidence matches with `tier`, `as_of`, and `source`
- Queue low-confidence or no-match entries for human review

### Daniel Review Flow
- Batch unclassified entries into Telegram message (grouped, not one per message)
- Show summary and candidate classes for each entry
- Implement response handling to apply classification
- Add newly discovered classes to registry and spec

### Completion Steps
- Verify all existing entries have `data_class`
- Confirm no unclassified entries remain
- Document new classes in `specs/memory-lifecycle.md`
- Make `data_class` required in function signatures

### Notes
- This will surface noise in existing data (e.g., "topic1" garbage entries, orphaned nodes) — expect some pruning during review
- The backfill conversation with Daniel is a form of the monthly review process — builds intuition for class coverage
