---
name: add-provider
description: Add a new AI provider to the system. Stores API key in keyring, adds provider config to config.yaml, and verifies connectivity.
argument-hint: "[provider-name]"
---

# add-provider

Add a new AI provider. `$ARGUMENTS[0]` = provider name (optional, ask if missing).

## Step 1 — Identify provider

Valid names: `anthropic`, `openai`, `azure-openai`, `ollama`, or a custom name for OpenAI-compatible endpoints.

## Step 2 — Gather config

- **Key-based providers** (anthropic, openai, azure-openai, custom): ask for API key
- **Ollama**: ask for endpoint URL (default: http://localhost:11434)
- **Azure OpenAI**: also ask for endpoint URL, deployment name, API version

## Step 3 — Store secrets

```bash
python3 tools/stateless/secrets/secrets.py set --key <PROVIDER>_API_KEY --value <key>
```

## Step 4 — Update config.yaml

Add the provider block under `providers:` with appropriate env overrides.

## Step 5 — Verify connectivity

Provider-specific health check:
- Anthropic: `curl -sf https://api.anthropic.com/v1/models -H "x-api-key: <key>" -H "anthropic-version: 2023-06-01"`
- OpenAI: `curl -sf https://api.openai.com/v1/models -H "Authorization: Bearer <key>"`
- Ollama: `curl -sf <endpoint>/api/tags`
- Azure: endpoint-specific verification
- Custom: `curl -sf <endpoint>/v1/models -H "Authorization: Bearer <key>"`

## Step 6 — Report

Provider added, key stored, connectivity verified.
