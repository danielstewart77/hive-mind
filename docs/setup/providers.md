# Providers and Model Configuration

## CLI-First Architecture

Hive Mind does **not** use the Anthropic Python SDK or the Claude API directly. Instead, each mind wraps the **Claude CLI** or **Codex CLI** in subprocess mode, inside its own container, via a shared harness module (`minds/harness/claude_cli.py` or `codex_cli.py`). The gateway (`hive-comms`) never spawns a CLI subprocess itself — it dispatches HTTP requests to whichever mind's container owns the session, reading that mind's `gateway_url` from the mind registry.

### Why CLI over SDK?

The Claude CLI provides capabilities the SDK does not expose:

- **Full Claude Code toolset** — file editing, shell execution, web search, and the entire built-in Claude Code toolchain are available out of the box
- **Session continuity** — the CLI manages its own session state, context window, and tool call loops; the harness module just relays messages
- **Self-improvement** — a mind can create new skills at runtime that become available in the same session, because the CLI re-reads its skill directory dynamically

The trade-off: the CLI is a subprocess, so the harness communicates over pipes rather than in-process function calls. This is intentional — it provides process isolation and makes the model runtime replaceable.

## Where provider selection actually lives

Two separate places, at two separate scopes:

**Per-mind, in `runtime.yaml`.** This is what actually decides whether a given mind talks to Anthropic, OpenAI, or a local Ollama model — see [specs/ollama-backed-mind.md](../../specs/ollama-backed-mind.md) for the exact fields (`provider:` + `env:`). The harness module reads this file at startup and builds the subprocess env from it, per mind, in that mind's own container. Env overrides are injected **per subprocess, never globally** — a compromised or misbehaving subprocess can't poison another mind's environment, and switching a mind's provider is just editing its `runtime.yaml` and recreating its container.

**The gateway's model registry, in `nervous-system/comms/config.yaml`.** This is a separate, repo-root-adjacent `providers`/`models` mapping (copy from `nervous-system/comms/config.yaml.example`) that only feeds `GET /models` and Ollama auto-discovery — it does not configure any mind's actual subprocess. (The repo root's own `config.yaml` has an identically-shaped `providers`/`models` block that looks like it should do this — it doesn't; nothing reads it. See [Configuration](configuration.md).)

```yaml
# nervous-system/comms/config.yaml
providers:
  anthropic: {}
  ollama:
    api_base: "http://host.docker.internal:11434"

models:
  sonnet: anthropic
  opus: anthropic
  haiku: anthropic
  # Ollama models are auto-discovered at startup and added here dynamically
```

**Model discovery**: `nervous-system/comms/models.py` queries `{api_base}/api/tags` (cached 60s) and registers every available Ollama model by its tag name (e.g. `llama3.1:8b`, `qwen2.5:14b`) alongside the static aliases above — this feeds `GET /models`, not any particular mind's config.

## Anthropic (Default)

Standard Claude Code via the Anthropic API. Requires Claude Code credentials (mounted from the host's `~/.claude`, or `CLAUDE_CODE_OAUTH_TOKEN` per-mind) rather than a bare API key. Uses static model aliases (`sonnet`, `opus`, `haiku`).

## Ollama (Local / Private)

A mind routes its Claude CLI through an Ollama-hosted model by setting two env vars in its own `runtime.yaml`:

```yaml
provider: ollama
env:
  ANTHROPIC_AUTH_TOKEN: ollama       # dummy token (Ollama doesn't validate it)
  ANTHROPIC_BASE_URL: http://host.docker.internal:11434
```

The Claude CLI sends requests in Anthropic API format; Ollama's OpenAI-compatible endpoint translates them. This works for text generation — tool-calling support depends on the model's capability. The Codex CLI harness has the equivalent pattern with `OLLAMA_BASE_URL` — see [specs/ollama-backed-mind.md](../../specs/ollama-backed-mind.md).

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

Or via slash command from any client: `/model opus`, `/model llama3.1:8b` (no argument lists available models). The session is killed and respawned with the new model. Conversation history is preserved via `--resume`.

## Adding a New Provider

1. Add a `providers.<name>` entry to `nervous-system/comms/config.yaml` if you want it to show up in `GET /models`'s static list.
2. For a mind to actually use it, set `provider: <name>` and the matching `env:` block in that mind's `runtime.yaml`.
3. If the provider needs dynamic model discovery, implement a method in `nervous-system/comms/models.py` (see the Ollama implementation for the pattern).

No harness code changes are required for providers that are API-compatible with the Anthropic or OpenAI Responses format — env overrides alone are sufficient.
