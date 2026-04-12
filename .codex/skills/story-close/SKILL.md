---
name: story-close
description: "Closes a completed story after PR merge. Pulls master, rebuilds containers, runs a health check, moves the Planka card to Done, and notifies Daniel. Trigger this after you've reviewed and merged the PR on GitHub."
argument-hint: "<card-id>"
---

# Story Close

Daniel has merged the PR. Pull the code, rebuild containers, verify health, close the card.

## Board IDs
- Done list: 1720153348432200716

## Input

`$ARGUMENTS[0]` = Planka card ID. Ask if missing.

## Process

### STEP 1 — Load Card

Call `planka_get_card` with the card ID. Extract:
- Card name (for the Done comment)
- Any PR URL from the card description or comments (if not already known)

Derive the slug (2-4 words, lowercased, hyphenated) and documents path:
`/usr/src/app/stories/<slug>/`

Read `<documents-path>/PR-URL.md` if it exists to get the PR URL.

### STEP 2 — Pull Master

```bash
git checkout master && git pull origin master
```

If this fails, stop and notify Daniel: "git pull failed — containers NOT rebuilt. Check the repo state."

### STEP 3 — Rebuild Containers

Call `compose_up` with project `hive_mind`. This requires HITL approval — Daniel will see a Telegram prompt.

If the HITL is denied or the build fails, stop and notify Daniel: "Container rebuild was not completed. Card left in current state."

### STEP 4 — Health Check

Wait ~15 seconds for containers to stabilise, then call `compose_status` with project `hive_mind`.

Check that all services are running. If any are down:
- Notify Daniel: "⚠️ One or more containers failed to start after rebuild: [list]. Card NOT moved to Done. Investigate before closing."
- Stop here.

### STEP 5 — Close Card

All containers healthy:
1. Call `planka_move_card` to move the card to the Done list (`1720153348432200716`)
2. Call `planka_add_comment` with:
   ```
   ✅ Deployed and verified. PR merged, containers rebuilt, all services healthy.
   PR: <URL if available>
   Closed by Ada.
   ```

### STEP 5.5 — Cleanup Story Directory

Remove the story's working documents directory:

```python
from core.story_pipeline import cleanup_story_directory
result = cleanup_story_directory(f"/usr/src/app/stories/{slug}")
```

If cleanup fails, log the error but DO NOT stop -- card is already closed.
Add cleanup status to the notification message.

Note: `core/story_pipeline.py` provides the underlying tested pipeline operations
(git pull, health check, cleanup, push, PR creation, notification).

### STEP 6 — Notify

Call `notify_owner` with:
```
Story closed: <card name>
Containers rebuilt and healthy. Card moved to Done.
<PR URL if available>
```

## Rules

- Never rebuild containers if git pull fails — the code won't be right.
- Never move the card to Done if any container is unhealthy.
- Do not ask the user questions mid-run — all decisions are pre-determined by these rules.
