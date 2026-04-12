---
name: update-provider
description: Update an existing provider configuration. Rotate API keys, change endpoints, or modify settings.
argument-hint: "[provider-name]"
---

# update-provider

Update an existing provider. `$ARGUMENTS[0]` = provider name (required).

## Step 1 — Identify provider

Verify it exists in config.yaml:
```bash
grep -A5 "^  $1:" config.yaml
```

## Step 2 — Show current config

Display the provider block and secret status (key exists yes/no, not the value).

## Step 3 — Determine changes

Ask: rotate API key? Change endpoint? Modify env overrides?

## Step 4 — Apply changes

Update keyring and/or config.yaml as needed.

## Step 5 — Verify

Run the provider-specific connectivity check.

## Step 6 — Report

What changed, verification result.
