# Providers and Model Configuration

## CLI-First Architecture

Hive Mind does **not** use the Anthropic Python SDK or the Claude API directly. Instead, it wraps the **Claude CLI** (`claude -p --stream-json`) in subprocess mode. Each session spawns a Claude CLI process; the gateway communicates with it over stdin/stdout using NDJSON.

### Why CLI over SDK?

The Claude CLI provides capabilities the SDK does not expose:

- **Full Claude Code toolset** — file editing, shell execution, web search, MCP tool integration, and the entire built-in Claude Code toolchain are available out of the box
- **MCP protocol** — the CLI handles the MCP client/server lifecycle natively; replicating this with the SDK would require implementing the full MCP protocol from scratch
- **Session continuity** — the CLI manages its own session state, context window, and tool call loops; the gateway just relays messages
- **Self-improvement** — Ada can create new MCP tools that are immediately available in the same session, because the CLI reloads tool registrations dynamically

The trade-off: the CLI is a subprocess, so the gateway communicates over pipes rather than in-process function calls. This is intentional — it provides process isolation and makes the Claude runtime replaceable.

## Provider Configuration

Providers are configured in `config.yaml` under the `providers` key. Each model alias maps to a provider name, and each provider optionally specifies environment overrides injected into the Claude CLI subprocess.

```yaml
providers:
  anthropic: {}          # no overrides needed — uses ANTHROPIC_API_KEY from keyring
  ollama:
    env:
      ANTHROPIC_AUTH_TOKEN: "ollama"
      ANTHROPIC_BASE_URL: "http://203.0.113.10:11434"
    api_base: "http://203.0.113.10:11434"

models:
  sonnet: anthropic      # claude-sonnet-4-6 via Anthropic
  opus: anthropic        # claude-opus-4-6 via Anthropic
  haiku: anthropic       # claude-haiku-4-5 via Anthropic
  # Ollama models are auto-discovered at startup and added here dynamically
```

### Per-Process Env Isolation

Environment overrides are injected **per subprocess** — never globally. The gateway reads the provider config for the requested model, builds an env dict, and passes it to `subprocess.Popen`. The parent process environment is never mutated. This means:

- Anthropic and Ollama sessions can run concurrently without interfering
- A compromised or misbehaving subprocess cannot poison the gateway's environment
- Switching providers mid-session is safe (kill the old process, spawn a new one with the correct env)

## Anthropic (Default)

Standard Claude Code via the Anthropic API. Requires `ANTHROPIC_API_KEY` in the keyring. Uses static model aliases (`sonnet`, `opus`, `haiku`) that map to the current best model in each tier.

## Ollama (Local / Private)

Ollama support routes Claude CLI through an Ollama-hosted model by overriding two env vars:

```yaml
ollama:
  env:
    ANTHROPIC_AUTH_TOKEN: "ollama"         # dummy token (Ollama doesn't validate it)
    ANTHROPIC_BASE_URL: "http://host:11434"  # points CLI at Ollama's API
```

The Claude CLI sends requests in Anthropic API format; Ollama's OpenAI-compatible endpoint translates them. This works for text generation — tool calling and MCP protocol support depend on the model's capability.

**Model discovery**: at startup, the model registry queries `api_base/api/tags` and registers all available Ollama models by their tag name (e.g. `llama3.1:8b`, `qwen2.5:14b`). These are added to the model map alongside the static Anthropic aliases.

**Use cases**:
- Private conversations (no data leaves the LAN)
- Cost-free experimentation
- Testing with specific open-source models
- Running tasks that don't need full Claude Code capability

## Switching Models

Models can be switched mid-session:

```http
POST /sessions/{id}/model
{"model": "llama3.1:8b"}
```

Or via slash command from any client:

```
/model opus
/model llama3.1:8b
```

The session is killed and respawned with the new provider's env. Conversation history is preserved via `--resume`.

## Ollama Plugin (Standalone)

For Claude Code users who want Ollama access **without running Hive Mind**, the `ollama-claude-plugin` is a standalone Claude Code plugin. It provides a `/ask-ollama` skill that delegates prompts to a local Ollama instance via HTTP, with conversation history managed by a broker daemon.

- **Repo:** `ollama-claude-plugin` (separate project)
- **Transport:** HTTP to `POST /api/chat` on the Ollama server
- **Multi-turn:** broker daemon accumulates messages per conversation ID
- **Model selection:** per-request via skill argument

Within Hive Mind, use **Bob** (the `cli_ollama` mind) for Ollama delegation rather than the standalone plugin.

## Adding a New Provider

1. Add a `providers.<name>` entry in `config.yaml` with any needed `env` overrides
2. Map model aliases to the provider name under `models`
3. If the provider needs dynamic model discovery, implement a `discover_models()` method in `core/models.py` (see the Ollama implementation for the pattern)

No code changes are required for providers that are API-compatible with the Anthropic format — env overrides alone are sufficient.
