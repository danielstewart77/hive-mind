---
name: setup-provider
description: Configure AI providers — Anthropic, OpenAI, Azure OpenAI, Ollama, or OpenAI-compatible endpoints. At least one provider is required. Stores API keys in the keyring and verifies connectivity.
argument-hint: "[provider-name]"
---

# setup-provider

Configure AI model providers. You need at least one.

## Step 1 — Check current state and present options

```bash
# Check which providers are already configured
grep -A5 "^providers:" config.yaml
```

Present all available providers with current status:

```
Available providers:
1. Anthropic          [not configured]  — Required for Claude CLI/SDK harnesses
2. OpenAI             [not configured]  — Required for Codex CLI harness native models
3. Azure OpenAI       [not configured]  — Enterprise/corporate OpenAI endpoints
4. Ollama             [not configured]  — Local model hosting (no API key needed)
5. OpenAI-compatible  [not configured]  — Generic endpoint (Groq, Together AI, vLLM, etc.)

You need at least one. Which would you like to configure?
(comma-separated numbers, or "all")
```

If `$ARGUMENTS[0]` is provided, skip to that provider directly.

## Step 2 — Anthropic (if selected)

Check for existing key: `python3 tools/stateless/secrets/secrets.py get --key ANTHROPIC_API_KEY`

If missing, ask user for their Anthropic API key. Store it:
```bash
python3 tools/stateless/secrets/secrets.py set --key ANTHROPIC_API_KEY --value <key>
```

Verify:
```bash
curl -sf https://api.anthropic.com/v1/models \
  -H "x-api-key: <key>" -H "anthropic-version: 2023-06-01" | jq ".data | length"
```

Add to config.yaml if not present:
```yaml
providers:
  anthropic: {}
models:
  sonnet: anthropic
  opus: anthropic
  haiku: anthropic
```

## Step 3 — OpenAI (if selected)

Check for existing key: `python3 tools/stateless/secrets/secrets.py get --key OPENAI_API_KEY`

If missing, ask user. Store it. Verify:
```bash
curl -sf https://api.openai.com/v1/models \
  -H "Authorization: Bearer <key>" | jq ".data | length"
```

Add to config.yaml:
```yaml
providers:
  openai:
    env:
      OPENAI_API_KEY: "<from-keyring>"
```

## Step 4 — Azure OpenAI (if selected)

Ask for: endpoint URL, API key, deployment name, API version.
Store API key in keyring. Verify endpoint connectivity.
Add to config.yaml with Azure-specific env overrides.

## Step 5 — Ollama (if selected)

Ask for endpoint URL (default: http://localhost:11434).
Check connectivity:
```bash
curl -sf <endpoint>/api/tags | jq ".models[].name"
```

If reachable, list available models. Add to config.yaml:
```yaml
providers:
  ollama:
    env:
      ANTHROPIC_AUTH_TOKEN: "ollama"
      ANTHROPIC_API_KEY: ""
      ANTHROPIC_BASE_URL: "<endpoint>"
    api_base: "<endpoint>"
```

## Step 6 — OpenAI-compatible (if selected)

Ask for: provider name (e.g. "groq"), endpoint URL, API key.
Store API key. Test connectivity using the OpenAI-compatible /v1/models endpoint.
Add to config.yaml as a named provider with env overrides.

## Step 7 — Summary

| Provider | Status | Details |
|----------|--------|---------|
| Anthropic | OK/FAIL | key stored, N models |
| OpenAI | OK/FAIL | key stored, verified |
| Ollama | OK/FAIL | endpoint, N models |
| ... | | |
