---
name: remove-mind
description: Remove a mind from the system. Deregisters from broker, stops containers, and optionally deletes the mind directory. Use when the user wants to remove a mind.
argument-hint: "[name]"
---

# remove-mind

`$ARGUMENTS[0]` = mind name. Required.

## Step 1 — Identify mind

Verify the mind exists in the broker:
```bash
curl -s http://localhost:8420/broker/minds | jq '.[] | select(.name == "<name>")'
```

If not in the broker, check if the directory exists:
```bash
ls minds/<name>/MIND.md
```

If neither exists, stop with error: "Mind '<name>' not found."

## Step 2 — Confirm with user

Display mind info (name, model, harness, gateway_url).

Ask for confirmation: "Remove mind '<name>'? This will deregister from broker and kill any running sessions."

Ask whether to delete the `minds/<name>/` directory or keep it for later re-registration.

## Step 3 — Kill running sessions

Check for running sessions:
```bash
curl -s http://localhost:8420/sessions | jq '.[] | select(.mind_id == "<name>" and .status == "running")'
```

Kill each one:
```bash
curl -s -X DELETE http://localhost:8420/sessions/<session_id>
```

## Step 4 — Deregister from broker

```bash
curl -s -X DELETE http://localhost:8420/broker/minds/<name>
```

Verify 200 response.

## Step 5 — Cleanup (if user chose to delete files)

```bash
rm -rf minds/<name>/
```

Verify deletion.

## Step 6 — Report

- Sessions killed (count)
- Broker deregistration status
- Files deleted or preserved
