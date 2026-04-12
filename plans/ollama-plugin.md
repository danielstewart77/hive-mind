# Ollama Plugin — Local Model Delegation for Claude Code

> **Status:** Research complete, ready for design.
> **Standalone plugin** — works with or without Hive Mind installed.

---

## Goal

Create a Claude Code plugin that lets any Claude Code user delegate work to a local Ollama instance. A skill or agent can invoke a local model (Llama, Qwen, Mistral, etc.) without leaving the Claude harness — useful for tasks where a local model is preferred (privacy, cost, speed, offline access).

## Architectural Pattern

This plugin follows the same pattern as OpenAI's `codex-plugin-cc` — a plugin that lets the host harness delegate work to an external provider it doesn't natively support.

| | codex-plugin-cc | ollama-plugin |
|---|---|---|
| **Host harness** | Claude Code | Claude Code |
| **External provider** | Codex CLI | Ollama API |
| **Transport** | JSON-RPC 2.0 over Unix domain sockets | HTTP to Ollama `/api/chat` endpoint |
| **Provider lifecycle** | Plugin manages Codex subprocess (start, stop, signal) | Ollama runs as a service — no lifecycle management needed |
| **Conversation state** | Managed by broker (Codex is stateless per-exec) | Managed by broker (Ollama is stateless per-request) |
| **Response bridging** | Codex JSON events → Claude Code event stream | Ollama JSON stream → Claude Code event stream |

### What codex-plugin-cc does (our template)

The plugin has a two-tier architecture:

1. **Broker daemon** — a detached background process that:
   - Communicates with the host harness via JSON-RPC 2.0 over Unix domain sockets
   - Manages the external provider subprocess (Codex) lifecycle
   - Handles conversation threading (maps conversation IDs to provider state)
   - Persists job state to JSON files for recovery
   - Supports graceful degradation to direct stdio pipes

2. **Skill/agent surface** — markdown files that the user invokes. The skill sends work to the broker, which delegates to the provider, collects the result, and returns it.

### How the Ollama plugin adapts this

**What stays the same:**
- Broker daemon pattern — a background process managing the provider connection
- JSON-RPC over Unix sockets for harness ↔ broker communication
- Conversation threading managed by the broker
- Job state persistence for recovery
- Skill/agent surface for user invocation

**What changes:**
- **No subprocess management.** Ollama runs as a service (`ollama serve`), not a subprocess the plugin spawns. The broker just needs to know the endpoint URL.
- **HTTP instead of stdio.** The broker talks to Ollama via `POST /api/chat` (streaming) or `POST /api/generate`. No Unix socket to the provider — just HTTP.
- **Conversation history.** Ollama's `/api/chat` accepts a `messages` array for multi-turn. The broker accumulates messages per conversation and sends the full history on each turn. This is the broker's primary job.
- **Model selection.** Ollama hosts multiple models. The broker (or skill) specifies which model to use per request.

### Simplified architecture

```
Claude Code (host harness)
  ↓ invokes skill
Ollama Plugin Skill (SKILL.md)
  ↓ JSON-RPC over Unix socket
Ollama Broker (background daemon)
  ↓ HTTP POST /api/chat
Ollama Service (local or remote)
  ↓ streams response
Broker collects → returns to skill → returns to harness
```

## Plugin Structure

```
ollama-plugin/
├── .claude-plugin/
│   └── plugin.json
├── skills/
│   ├── ollama-query/SKILL.md       # Send a prompt to a local model
│   ├── ollama-models/SKILL.md      # List available models
│   └── ollama-config/SKILL.md      # Configure endpoint, default model
├── agents/
│   └── ollama-worker.md            # Subagent that delegates to Ollama
├── broker/
│   ├── daemon.py                   # JSON-RPC broker daemon
│   ├── ollama_client.py            # HTTP client for Ollama API
│   └── conversation_store.py       # Per-conversation message history
├── .mcp.json                       # Optional MCP server wrapping the broker
└── README.md
```

## Key Design Decisions

1. **Broker manages conversation history.** Ollama is stateless — each `/api/chat` call needs the full message array. The broker accumulates messages per conversation ID and sends the full history on each turn.

2. **Model is per-request.** The skill or agent specifies the model (`llama3`, `qwen3`, `mistral`, etc.). The broker passes it through to Ollama. A default model can be configured.

3. **Streaming.** Ollama supports streaming responses (`stream: true` in the API). The broker should stream back to the harness rather than buffering the full response.

4. **Endpoint is configurable.** Default `http://localhost:11434`, but configurable for remote Ollama instances. Stored in plugin config, not hardcoded.

5. **Standalone.** No dependency on Hive Mind. Any Claude Code user can install this plugin and delegate to a local Ollama instance.

## Open Questions

- **MCP vs broker daemon?** Could the broker be implemented as an MCP server instead of a separate daemon? The skill would call MCP tools rather than JSON-RPC. Simpler integration, but MCP tools are request/response — streaming is harder.
- **Multi-turn UX.** How does the user continue a conversation with the local model? Explicit conversation IDs in the skill, or implicit (the broker tracks the "current" conversation per session)?
- **Error handling.** What happens if Ollama is down? The broker should return a clear error, and the skill should surface it — not hang.
- **Context window management.** Long conversations will exceed the local model's context window. Should the broker truncate, summarize, or error when this happens?
