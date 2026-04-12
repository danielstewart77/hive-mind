---
name: update-mind
description: Update an existing mind's configuration. Supports partial updates to model, harness, gateway_url, and soul seed. Use when the user wants to change a mind's settings.
argument-hint: "[name]"
---

# update-mind

`$ARGUMENTS[0]` = mind name. Required.

## Step 1 — Identify mind

Verify the mind exists:
```bash
curl -s http://localhost:8420/broker/minds | jq '.[] | select(.name == "<name>")'
```

If not found, stop with error: "Mind '<name>' not registered. Use `/list-minds` to see available minds."

Also read the current MIND.md:
```bash
cat minds/<name>/MIND.md
```

## Step 2 — Determine changes

Ask the user what to change:
- Model
- Harness
- Gateway URL
- Soul seed (the markdown body of MIND.md)

## Step 3 — Update MIND.md

Read current `minds/<name>/MIND.md`, parse the frontmatter, apply the requested changes, write it back. Preserve the soul seed unless the user explicitly wants to change it.

## Step 4 — Update broker

```bash
curl -s -X PUT http://localhost:8420/broker/minds/<name> \
  -H "Content-Type: application/json" \
  -d '{"model": "...", "harness": "...", "gateway_url": "..."}'
```

Only include fields that changed. Verify 200 response.

## Step 5 — Side effects

- If **harness changed**: warn the user that `implementation.py` may need updating. Offer to copy the matching template from `mind_templates/`.
- If **gateway_url changed**: verify the new URL is reachable.
- If **model changed** and mind has running sessions: warn the user to restart sessions for the change to take effect.

## Step 6 — Report

Fields changed, broker updated, any warnings issued.
