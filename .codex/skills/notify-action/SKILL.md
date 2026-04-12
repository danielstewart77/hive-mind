---
name: notify-action
description: Procedure for handling a memory chunk with the notify action. Use when save-memory encounters a chunk that requests a future alert. Determines whether to call set_reminder, add a scheduler entry, or inform Daniel the feature is unsupported.
---

# Notify Action

The notify action means: this chunk is a request for a future alert. Determine which case applies.

## Case 1 — One-time reminder

Criteria: single specific future datetime, no recurrence pattern.
Examples: "remind me at 10am about the mulch delivery", "remind me tomorrow to call the doctor"

1. Extract message and target time from the chunk.
2. Call `set_reminder(message, when)`.
3. Done — SQLite row self-destructs after firing.

Edge case — time already past: discard the notify action. Inform Daniel it was skipped and why.

## Case 2 — Daily recurring

Criteria: request to fire at the same time every day, indefinitely.
Examples: "remind me every morning at 7am to take my vitamins"

1. Extract message and daily time.
2. Add entry to `config.yaml` under `scheduled_tasks` with appropriate cron (e.g., `0 7 * * *`).
3. Notify Daniel: this is added and requires a container restart to take effect.
4. Note: this runs indefinitely — removal requires deleting from `config.yaml` and restarting.

## Case 3 — Non-daily recurring (unsupported)

Criteria: recurrence pattern that is not daily — weekly, monthly, yearly, custom (e.g., "every July 13th").

→ Feature does not exist yet. Tell Daniel:
"Recurring reminders with patterns other than daily are not yet supported. Your reminder has not been set. See backlog card: [Feature] Recurring Reminders / Timed Events."

Do not write to `set_reminder` or `config.yaml`. Do not silently discard.

Update this spec when the recurring reminder feature is built.
