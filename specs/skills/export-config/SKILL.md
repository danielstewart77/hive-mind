---
name: export-config
description: Export the current Hive Mind configuration for migration or duplication. Produces a portable config bundle that /setup-config can import on another machine.
user-invocable: true
tools: Bash, Read
---

# export-config

Export the current deployment configuration for migration or duplication.

## Step 1 — Scan current state

Detect all installed components:
```bash
# Providers
grep -A1 "^providers:" config.yaml

# Minds
curl -sf http://localhost:8420/broker/minds | jq -r ".[].name"

# Running containers
docker compose ps --format "{{.Name}} {{.Status}}"

# Compose profile
grep COMPOSE_PROFILES .env 2>/dev/null
```

## Step 2 — Export config files

```bash
mkdir -p hive-mind-export
cp config.yaml hive-mind-export/
cp .env hive-mind-export/ 2>/dev/null
cp .mcp.json hive-mind-export/ 2>/dev/null
cp .mcp.container.json hive-mind-export/ 2>/dev/null
```

Sanitize .env — replace secret values with placeholders.

## Step 3 — Export mind definitions

```bash
for d in minds/*/; do
  name=$(basename "$d")
  [[ "$name" == "__pycache__" ]] && continue
  if [ -f "$d/MIND.md" ]; then
    mkdir -p "hive-mind-export/minds/$name"
    cp "$d/MIND.md" "hive-mind-export/minds/$name/"
  fi
done
```

Copy MIND.md files only (not implementation.py — that comes from templates).

## Step 4 — Export secret manifest

List all secret keys in the keyring (names only, not values):
```bash
python3 tools/stateless/secrets/secrets.py list > hive-mind-export/secret-manifest.txt
```

## Step 5 — Export compose profile

```bash
echo "profile: ${COMPOSE_PROFILES:-cpu-only}" > hive-mind-export/profile.txt
```

## Step 6 — Bundle

```bash
tar czf hive-mind-export.tar.gz hive-mind-export/
rm -rf hive-mind-export/
```

## Step 7 — Report

What was exported, file location, how to import:
"To import on another machine: `/setup-config --import hive-mind-export.tar.gz`"
