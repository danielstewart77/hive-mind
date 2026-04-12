---
name: remove-provider
description: Remove a provider from the system. Warns if minds depend on it. Cleans up secrets and config.
argument-hint: "[provider-name]"
---

# remove-provider

Remove a provider. `$ARGUMENTS[0]` = provider name (required).

## Step 1 — Identify provider

Verify it exists in config.yaml.

## Step 2 — Check dependencies

Query the broker for minds whose model maps to this provider:
```bash
curl -sf http://localhost:8420/broker/minds | jq ".[] | select(.harness | contains(\"$provider\"))"
```

If minds depend on this provider, warn: "The following minds use this provider: <list>. Removing it will break them."

## Step 3 — Confirm

Ask user to confirm removal.

## Step 4 — Remove from config.yaml

Delete the provider block from `providers:` and any model mappings.

## Step 5 — Clean up secrets

Ask: "Remove the API key from the keyring too?"
If yes:
```bash
python3 tools/stateless/secrets/secrets.py delete --key <PROVIDER>_API_KEY
```

## Step 6 — Report

Provider removed, dependencies warned, secrets cleaned (or preserved).
