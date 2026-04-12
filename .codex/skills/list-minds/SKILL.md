---
name: list-minds
description: List all registered minds with status information. Use when the user asks what minds are available or wants to see the mind registry.
---

# list-minds

Query the broker for all registered minds and display them.

## Step 1 — Fetch and display

```bash
curl -s http://localhost:8420/broker/minds | jq .
```

Format each mind as a table:

| Name | Model | Harness | Gateway URL | Last Seen |
|------|-------|---------|-------------|-----------|

Convert `last_seen` (unix timestamp) to human-readable time.

If the list is empty, tell the user: "No minds registered. Use `/add-mind` or `/create-mind` to register one."
