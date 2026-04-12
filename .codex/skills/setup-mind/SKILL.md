---
name: setup-mind
description: Set up minds for the Hive Mind system. Create new minds, import existing ones, or re-register discovered minds. Configures per-mind volumes and authentication.
---

# setup-mind

Set up minds for the system. At least one mind is needed.

## Step 1 — Prerequisite check

```bash
curl -sf http://localhost:8420/sessions > /dev/null || echo "Gateway not reachable. Run /setup-nervous-system first."
curl -sf http://localhost:8420/broker/minds | jq length
```

Verify at least one provider is configured (check config.yaml providers block).

## Step 2 — List current minds

```bash
# Registered minds
curl -sf http://localhost:8420/broker/minds | jq -r ".[].name"

# Unregistered mind folders
for d in minds/*/; do
  name=$(basename "$d")
  [[ "$name" == "__pycache__" ]] && continue
  if [ -f "$d/MIND.md" ]; then
    registered=$(curl -sf http://localhost:8420/broker/minds | jq -r ".[] | select(.name==\"$name\") | .name")
    [ -z "$registered" ] && echo "UNREGISTERED: $name (has MIND.md)"
  fi
done
```

## Step 3 — Present options

1. **Create a new mind from template** → delegates to `/create-mind`
2. **Add an existing local mind** → delegates to `/add-mind`
3. **Connect a remote mind** → delegates to `/add-mind` (remote scenario)
4. **Import a mind from another Hive Mind instance** → ask for the `minds/<name>/` folder path or archive, copy it in, delegate to `/add-mind` for registration
5. **Re-register a discovered mind** (folder exists, not in broker) → delegates to `/add-mind`
6. **Skip** — no changes needed

## Step 4 — Execute

Delegate to the appropriate skill based on user choice.

## Step 5 — Per-mind volume config

Ask what host directories this mind should access:
- Internal-only volumes (default Docker named volume, most isolated)
- Host directory mounts: which paths, read-only or read-write?

## Step 6 — Per-mind auth

If per-mind auth model was chosen in `/setup-auth`:
- Guide user through authenticating inside the new mind container:
  ```bash
  docker exec -it <container-name> claude
  ```
- Or for API key: configure the key in the mind container env
- Verify auth works

## Step 7 — Repeat or finish

Ask: "Would you like to set up another mind?"
- If yes → return to Step 3
- If no → present final mind roster and exit
