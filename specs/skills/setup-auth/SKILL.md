---
name: setup-auth
description: Set up Claude Code authentication for Hive Mind. Choose isolation model (shared vs per-mind) and auth method (OAuth vs API key). Explains pros and cons of each combination.
user-invocable: true
tools: Bash, Read
---

# setup-auth

Configure Claude Code authentication for the Hive Mind system.

## Step 1 — Explain auth models

Present two independent decisions:

### Decision 1: Isolation Model

**Shared auth (simpler):**
- One Claude OAuth token or API key used by all minds
- Host `~/.claude/` mounted into containers
- Pros: one login, easy to manage, quick to set up
- Cons: all minds share session history, memory, and credentials. A compromised mind sees everything.
- Best for: single-user setups, getting started quickly

**Per-mind auth (recommended for production):**
- Each mind has its own named volume with independent auth
- `CLAUDE_CONFIG_DIR` set per container
- Pros: full isolation — separate memory, sessions, credentials per mind
- Cons: must authenticate each mind individually
- Best for: production deployments, multi-mind systems, security-conscious setups

### Decision 2: Auth Method

**OAuth (subscription users — Claude Max/Pro):**
- Interactive browser login flow
- Token stored in `~/.claude.json`
- Pros: no API key to manage, uses existing subscription
- Cons: requires browser access for initial setup, token can expire

**API key (pay-per-use):**
- Set `ANTHROPIC_API_KEY` environment variable or write to `~/.claude.json`
- Pros: simple, scriptable, no browser needed
- Cons: pay-per-use costs, key management

**None (Ollama-only users):**
- If using only Ollama as a provider, no Claude auth is needed
- The Claude CLI still needs to be installed but auth is handled by the Ollama provider env vars

## Step 2 — Ask user for choices

Ask isolation model preference (recommend per-mind).
Ask auth method based on their provider situation:
- Using Anthropic? → OAuth or API key
- Using OpenAI/Codex? → API key (stored separately)
- Using only Ollama? → No auth needed

## Step 3 — Execute chosen path

**Shared + OAuth:**
- Check if `~/.claude.json` exists on host
- If not, guide: "Run `claude` in your terminal to complete the OAuth login flow"
- Verify: `claude --version` works without auth errors

**Shared + API key:**
- Ask for the API key
- Store via secrets tool
- Write to `~/.claude.json` or set in container env

**Per-mind + OAuth:**
- Explain that each mind container will need individual auth
- This happens during `/setup-mind` — each new mind gets authenticated after container creation
- For now, just record the choice

**Per-mind + API key:**
- Store the API key in the keyring
- Each mind container will reference it from the keyring at startup
- Record the choice

## Step 4 — Report

Auth model chosen, method selected, token/key verified (if applicable), next steps.
