# [Memory] Monthly Review Pass — World-Events, Intentions, Session-Logs

**Card ID:** 1723686142072587807

## Description

Monthly scheduler job that surfaces `world-event`, `intention`, and `session-log` entries for Daniel's review. Daniel decides: keep, archive, or discard each one. This is the human-in-the-loop pass for data that can't be auto-pruned.

Depends on: Schema & Metadata story.

## Acceptance Criteria

- [x] Scheduler job runs monthly and queries all entries with `data_class` in (`world-event`, `intention`, `session-log`) that haven't been reviewed in the past 30 days
- [x] Batches entries by class and sends Daniel a Telegram review message
- [x] Review message format is grouped by class (world-events together, intentions together, session-logs together)
- [x] Each entry includes brief summary + date stored + options (keep / archive / discard)
- [x] Single batched message or thread per class group (not one message per entry)
- [x] Keep response sets `last_reviewed_at = now`, no other change
- [x] Archive response (world-event only) moves entry to long-term document store, removes from active vector store, marks `archived=True`
- [x] Discard response deletes entry from vector store and/or graph
- [x] Long-term archive store for world-events is implemented (placeholder using JSON file in `/data/` with design for later migration)
- [x] `memory_retrieve` excludes archived entries by default and supports `include_archived=True` flag
- [x] Tests verify monthly job correctly identifies entries due for review
- [x] Tests verify Keep updates `last_reviewed_at`
- [x] Tests verify Archive moves entry to archive store and removes from active retrieval
- [x] Tests verify Discard removes entry entirely
- [x] Tests verify `memory_retrieve` excludes archived entries by default

## Tasks

### 1. Scheduler job (monthly)
- Implement monthly scheduler job in `clients/scheduler.py` or dedicated module
- Query all entries with `data_class` in (`world-event`, `intention`, `session-log`) where `last_reviewed_at` is null or older than 30 days
- Batch entries by class

### 2. Review message format & sending
- Create review message builder that groups entries by class
- Each entry: brief summary + date stored + inline keyboard buttons (keep / archive / discard)
- Send single batched message per class group via Telegram
- Implement callback handler for button responses

### 3. Response handling
- **Keep**: Update `last_reviewed_at = now()` in Neo4j/vector store
- **Archive**: Move to JSON file store at `/data/world_events_archive.json`, mark `archived=True` in metadata, remove from `memory_retrieve` default results
- **Discard**: Delete from Neo4j and vector store

### 4. Long-term archive store (world-event)
- Implement JSON file-based placeholder at `/data/world_events_archive.json`
- Schema: `{ "archived_at": ISO8601, "original_entry": {...}, "reason": "archived" }`
- Design for migration (clear interface, easy to swap for SQLite table or separate Neo4j label later)

### 5. Update `memory_retrieve`
- Add `include_archived=False` parameter
- Filter out archived entries by default when searching

### 6. Tests
- Unit tests for scheduler job query logic
- Unit tests for batch/message building
- Integration tests for callback handling (keep/archive/discard)
- Tests for archive store persistence
- Tests for `memory_retrieve` filtering
