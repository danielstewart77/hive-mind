# Data Class: ephemeral

## Description
Point-in-time data that was only accurate when retrieved and has no lasting relevance. Recognizable by a specific timestamp and data that changes constantly — weather, live prices, current system state, "what is X right now" query results. No durable fact can be extracted from it.

## Actions
- discard

## Notes
- Always discard — no human verification needed
- Examples: weather lookups, crypto/stock price checks, live API status results, one-time query snapshots
- If a fact derived from ephemeral data IS durable (e.g. "Daniel prefers to check weather before outdoor plans"), that fact belongs in `preference`, not here
- Distinguished from `timed-event` (which is a future scheduled event worth tracking until it occurs)
