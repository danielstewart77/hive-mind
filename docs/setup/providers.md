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
```

**Model discovery**: an Ollama model is reachable once it has a row in the inference proxy pointing at the Ollama provider. The proxy's listing is what a mind reports at `GET /models` and what the console offers — a tag pulled on the box but never registered is not addressable, and is therefore not offered.

## Anthropic (Default)

Standard Claude Code via the Anthropic API. Requires Claude Code credentials (mounted from the host's `~/.claude`, or `CLAUDE_CODE_OAUTH_TOKEN` per-mind) rather than a bare API key. Models are named by their proxy deployment name (`claude-opus-5`, `claude-sonnet-5`), which is what the proxy routes on.

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

Or via slash command from any client: `/model claude-opus-5`, `/model qwen35-131k` (no argument lists what this mind may run). A model the mind's own proxy key cannot address is refused rather than spawned. The session is killed and respawned with the new model. Conversation history is preserved via `--resume`.

## Adding a New Provider

1. Add the provider in the inference proxy's admin console (`/admin/providers`): its base URL, its credential, and one path per request shape it serves. A provider that serves both `/v1/messages` and `/v1/responses` is offered to claude and codex minds alike.
2. Add its models at `/admin/models`, each pointing at that provider. Restrict a model to particular harnesses only to withhold it from one it would otherwise reach.
3. For a mind to use it, set `provider: <name>` and the model's deployment name in that mind's `runtime.yaml` — or pick both from the console's Mind page, which writes the same file.

No code changes anywhere. The gateway holds no model table, and the harnesses reach every provider through the proxy.
