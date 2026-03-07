# [Memory] Timed-Event Auto-Expiry — Nightly Pass

**Card ID:** 1723685753000560150

## Description

Implement the only fully automated pruning pass: timed-event expiry. Entries in the `timed-event` class set their own expiry by nature of their data — the event datetime, resolved to absolute at write time.

**Depends on:** Schema & Metadata story.

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

## Tasks

### Write-time Enforcement
- Modify `memory_store` to validate `timed-event` entries
- Require resolved absolute `expires_at` datetime
- Return error on unresolved time references
- Add `recurring` boolean flag (default `False`)

### Nightly Scheduler Job (Pass 1)
- Query all `timed-event` entries where `expires_at < now`
- Delete non-recurring entries unconditionally
- For recurring entries: send Telegram prompt to Daniel with summary and date
- Implement logging for all deletions

### Recurring Event Detection
- Implement keyword heuristics: birthday, anniversary, weekly, monthly, annual, every, recurring
- Allow explicit `recurring=True` override at write time

### Testing
- Test nightly job deletion of expired non-recurring events
- Test nightly job Telegram prompt for expired recurring events
- Test `memory_store` rejection of entries with unresolved time references
