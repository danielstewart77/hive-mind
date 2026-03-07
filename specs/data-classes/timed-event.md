# Data Class: timed-event

## Description
A future scheduled event with a specific date and time — recognizable by a concrete datetime reference and a named occurrence. Exists only until the event occurs, then expires.

## Actions
- save-vector
- notify

## Notes
- REQUIRED: expires_at must be set to an absolute ISO 8601 datetime (e.g. "2026-04-01T15:00:00Z")
- Distinguished from `intention` (no specific datetime) and `world-event` (already occurred)
- Recurring events (birthday, anniversary, weekly meeting) must be flagged recurring=true
- Examples: scheduled meetings, appointments, reminders with specific datetimes, recurring birthdays
- After the event time passes, the entry is eligible for monthly review deletion
