# Refactor Proposal: Hive Mind v2 — Claude Code as the CPU

## Vision

Hive Mind today is a collection of brittle, hand-written tools glued together by a tag-based triage system (GPT classifies your message into a tag, then an LLM picks a tool from that tag). Most of these tools do things Claude Code already does natively — file ops, git, web search, code generation — but worse, because they're frozen in time and riddled with hardcoded paths, broken imports, and dead code.

The refactored Hive Mind flips the architecture:

```
User (web UI / terminal / voice)
        |
   Web App (FastAPI + WebSocket)  ←or→  terminal_app.py (REPL + STT/TTS)
        |
   Claude Code SDK  (the "CPU" — handles 90% of requests directly)
        |                           ↕  configurable backend (Anthropic or Ollama)
   MCP Tools        (the "peripherals" — only for things Claude Code can't do alone)
```

Claude Code becomes the brain. It can already read/write files, run git commands, search the web, generate code, run tests, and reason about complex tasks. The interfaces — a web app for production/Docker and a terminal REPL for local dev — are thin shells that capture input (text or voice), pass it to Claude Code, and stream the response back (text or audio).

Tools only exist for capabilities that require **secrets, external service connections, or stateful integrations** that Claude Code can't access on its own. These are exposed as MCP tools so Claude Code can call them when needed.

The self-improving capability remains: if Hive Mind can't handle a request, it creates a new MCP tool on the fly — with user input when secrets are needed, without when they're not.

The backend is configurable: Claude Code talks to Anthropic by default, but the entire SDK can be pointed at an Ollama instance (or any OpenAI-compatible server) by setting environment variables — enabling fully local, cost-free operation when you don't need Anthropic's full capabilities.

---

## What Changes

### The Core Loop

**Before:** User message → GPT triage (classify into tag) → agent_tooling selects tool → tool executes → stream response

**After:** User message → Claude Code SDK → Claude Code handles it (calling MCP tools if needed) → stream response

The entire triage system, tag classification, OpenAI/Ollama tooling layer, and LangGraph workflow machinery gets removed. The core `process_message()` is shared between both interfaces:

```python
def process_message(user_input: str, on_chunk=None) -> str:
    """Send user input to Claude Code and stream the response.

    Args:
        on_chunk: Callback for each text chunk. Terminal prints to stdout,
                  web app sends over WebSocket.
    """
    messages.append({"role": "user", "content": user_input})

    full_response = ""
    for chunk in invoke_claude_code(
        prompt=user_input,
        system_prompt=SYSTEM_PROMPT,
        chat_history=messages,
    ):
        if on_chunk:
            on_chunk(chunk)
        full_response += chunk

    messages.append({"role": "assistant", "content": full_response.strip()})
    return full_response.strip()
```

The SYSTEM_PROMPT tells Claude Code:
- You are Hive Mind, a personal assistant
- You have MCP tools available for external services (crypto prices, weather, Neo4j, etc.)
- If the user asks for a capability you don't have, create a new MCP tool for it
- Here's how to create tools (the @tool decorator pattern, file location, discover_tools reload)

### Web Interface (web_app.py) — NEW

The terminal REPL works great for local development, but doesn't work inside Docker containers or for remote access. A web interface provides the same functionality in a browser:

**Technology:** FastAPI + WebSocket + vanilla JS (or lightweight framework like htmx/Alpine.js)

**Why WebSocket:** Streaming is the core UX. Claude Code yields chunks of text as it works — WebSocket pushes these to the browser in real time, exactly like the terminal's `print(chunk, end="", flush=True)`.

**Features (matching terminal parity):**

| Terminal | Web |
|---|---|
| Text input via stdin | Text input via chat box |
| Streaming stdout | Streaming via WebSocket |
| `/voice` spacebar recording | Browser microphone (MediaRecorder API) |
| TTS via sounddevice | TTS via Web Audio API or `<audio>` element |
| `/model`, `/status`, `/tools` | Settings panel / sidebar |
| `/clear` | Clear button |
| Chat history in memory | Chat history in session (same backend list) |

**Architecture:**

```python
# web_app.py
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/static", StaticFiles(directory="web/static"), name="static")

@app.get("/")
async def index():
    return FileResponse("web/index.html")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_json()

        if data["type"] == "message":
            # Stream response chunks back over WebSocket
            async for chunk in invoke_claude_code_async(data["content"], ...):
                await websocket.send_json({"type": "chunk", "content": chunk})
            await websocket.send_json({"type": "done"})

        elif data["type"] == "audio":
            # Receive audio blob, transcribe, process, return audio
            audio_bytes = base64.b64decode(data["audio"])
            text = transcribe_audio(audio_bytes)
            await websocket.send_json({"type": "transcription", "content": text})
            # Then process as normal message...

        elif data["type"] == "command":
            handle_command(data["command"])
```

**Web UI (`web/index.html` + `web/static/`):**
- Clean chat interface with message bubbles (markdown rendered)
- Input box with send button and mic button
- Mic button: hold to record, release to send (mirrors spacebar behavior)
- Settings gear: backend selection (Anthropic/Ollama), model name, TTS voice
- Tool list sidebar (collapsible)
- Responsive — works on mobile for voice assistant use

**Voice in the browser:**
- **STT**: Browser's MediaRecorder API captures audio → send as base64 over WebSocket → server transcribes via OpenAI Whisper (same as terminal)
- **TTS**: Server generates audio via OpenAI TTS → send audio bytes over WebSocket → browser plays via Web Audio API
- Alternative: use browser-native Web Speech API for STT/TTS to reduce API costs (configurable)

**Docker deployment:**
```yaml
# docker-compose.yml
services:
  hive-mind:
    build: .
    ports:
      - "7780:7780"    # Web UI
      - "7777:7777"    # MCP server (internal)
    volumes:
      - ~/.claude:/root/.claude:ro  # Claude credentials
      - ./.env:/app/.env:ro
    command: uvicorn web_app:app --host 0.0.0.0 --port 7780
```

### Configurable Backend (Anthropic / Ollama) — NEW

The Claude Code SDK uses environment variables to determine its backend. By setting three variables, the entire system can point at an Ollama instance instead of Anthropic:

```bash
# .env or docker environment
# --- Backend Configuration ---
# Default: Anthropic (no overrides needed, just set ANTHROPIC_API_KEY)

# To use Ollama instead:
# ANTHROPIC_AUTH_TOKEN=ollama
# ANTHROPIC_API_KEY=
# ANTHROPIC_BASE_URL=http://203.0.113.10:11434
```

**Implementation — `config.py` (NEW):**

```python
# config.py — Centralized configuration
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

@dataclass
class HiveMindConfig:
    # Backend selection
    backend: str = "anthropic"  # "anthropic" or "ollama"

    # Model selection
    model: str = ""  # Active model. Empty = backend default.

    # Anthropic settings
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5-20250929"  # Default Anthropic model

    # Ollama settings
    ollama_server: str = "localhost"
    ollama_port: int = 11434
    ollama_model: str = "qwen3:8b"  # Default Ollama model

    # OpenAI settings (for STT/TTS regardless of backend)
    openai_api_key: str = ""

    # MCP
    mcp_port: int = 7777

    # Web UI
    web_port: int = 7780

    @property
    def active_model(self) -> str:
        """The model currently in use. Falls back to the backend's default."""
        if self.model:
            return self.model
        return self.anthropic_model if self.backend == "anthropic" else self.ollama_model

    def apply_backend_env(self):
        """Set environment variables for the Claude Code SDK based on backend choice."""
        if self.backend == "ollama":
            os.environ["ANTHROPIC_AUTH_TOKEN"] = "ollama"
            os.environ["ANTHROPIC_API_KEY"] = ""
            os.environ["ANTHROPIC_BASE_URL"] = f"http://{self.ollama_server}:{self.ollama_port}"
        else:
            # Anthropic mode — just ensure the API key is set
            if self.anthropic_api_key:
                os.environ["ANTHROPIC_API_KEY"] = self.anthropic_api_key
            # Clear any Ollama overrides from a previous switch
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            os.environ.pop("ANTHROPIC_BASE_URL", None)

    def switch_backend(self, backend: str, server: str = None, model: str = None):
        """Switch backend at runtime. Re-exports env vars immediately."""
        self.backend = backend
        if server:
            self.ollama_server = server
        if model:
            self.model = model
        else:
            self.model = ""  # Reset to backend default
        self.apply_backend_env()

    def switch_model(self, model: str):
        """Switch model at runtime without changing backend."""
        self.model = model

    @classmethod
    def from_env(cls) -> "HiveMindConfig":
        """Load configuration from environment variables."""
        return cls(
            backend=os.getenv("HIVE_MIND_BACKEND", "anthropic"),
            model=os.getenv("HIVE_MIND_MODEL", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
            ollama_server=os.getenv("OLLAMA_SERVER", "localhost"),
            ollama_port=int(os.getenv("OLLAMA_PORT", "11434")),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            mcp_port=int(os.getenv("MCP_PORT", "7777")),
            web_port=int(os.getenv("WEB_PORT", "7780")),
        )

config = HiveMindConfig.from_env()
config.apply_backend_env()
```

**Usage in `.env`:**

```ini
# === Hive Mind Configuration ===

# Backend: "anthropic" (default) or "ollama"
HIVE_MIND_BACKEND=anthropic

# Model override (optional — leave empty to use the backend's default)
# HIVE_MIND_MODEL=

# Anthropic (when backend=anthropic)
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929   # Default Anthropic model

# Ollama (when backend=ollama)
OLLAMA_SERVER=203.0.113.10
OLLAMA_PORT=11434
OLLAMA_MODEL=qwen3:8b                        # Default Ollama model

# OpenAI (always needed for STT/TTS voice features)
OPENAI_API_KEY=sk-...

# Ports
MCP_PORT=7777
WEB_PORT=7780
```

**Switching backends and models at runtime** via the web UI settings panel or terminal commands:

```
/backend ollama                    # Switch to Ollama (uses OLLAMA_MODEL default)
/backend anthropic                 # Switch back to Anthropic (uses ANTHROPIC_MODEL default)
/backend ollama 203.0.113.10      # Switch to Ollama at specific server

/model qwen3:14b                   # Switch model within current backend
/model claude-opus-4-6            # Switch model within current backend

/status                            # Shows: backend, model, server, voice, message count
```

The `/backend` command re-exports the environment variables and reinitializes the Claude Code SDK connection. The `/model` command just changes which model the SDK requests — no reconnection needed. Both the terminal and web UI settings panel use the same `config.switch_backend()` / `config.switch_model()` methods.

**How it flows through to the SDK:**

```python
# In services/claude_code.py — invoke_claude_code() reads config.active_model
from config import config

options = ClaudeAgentOptions(
    model=config.active_model,     # "qwen3:8b" or "claude-sonnet-4-5-20250929" etc.
    cwd=PROJECT_ROOT,
    system_prompt=system_prompt,
    permission_mode=permission_mode,
    mcp_servers=[...],
)
```

When the user switches models at runtime, the next `invoke_claude_code()` call picks up the new value from `config.active_model` automatically — no restart or reconnection needed.

**Docker with Ollama:**

```yaml
services:
  hive-mind:
    environment:
      - HIVE_MIND_BACKEND=ollama
      - OLLAMA_SERVER=203.0.113.10
      - OLLAMA_PORT=11434
      - OPENAI_API_KEY=${OPENAI_API_KEY}  # Still needed for voice
```

**Trade-offs by backend:**

| Capability | Anthropic | Ollama |
|---|---|---|
| Default model | `claude-sonnet-4-5-20250929` | `qwen3:8b` |
| Model switching | Any Anthropic model | Any Ollama-pulled model |
| Code generation quality | Excellent | Varies by model |
| Web search | Built-in | Not available |
| Tool use / MCP | Full support | Depends on model |
| File editing | Full support | Depends on model |
| Cost | Per-token API cost | Free (local compute) |
| Latency | Network-dependent | LAN/local |
| Privacy | Data sent to Anthropic | Fully local |

**Note:** When using Ollama, some Claude Code native capabilities (WebSearch, advanced tool use) may be degraded or unavailable depending on the model. The MCP tools become more important in this mode as they provide structured capabilities the local model can call. Models with strong tool-use support (qwen3, llama3.1, mistral) will work better than pure completion models.

### What Stays

| Component | Status | Why |
|---|---|---|
| `terminal_app.py` | **Keep, simplify** | The REPL shell for local dev — simplified to just pass-through to Claude Code SDK |
| `services/speech.py` | **Keep as-is** | STT/TTS via OpenAI APIs — Claude Code can't hear or speak. Used by both terminal and web app |
| `services/claude_code.py` | **Keep, enhance** | The SDK bridge — needs chat history support, MCP tool awareness, and backend configurability |
| `mcp_server.py` | **Keep, simplify** | Exposes remaining tools as MCP endpoints for Claude Code to call |
| `agents/coingecko.py` | **Keep as MCP tool** | Calls CoinGecko API with optional API key — specific integration |
| `agents/get_weather_for_location.py` | **Keep as MCP tool** | Calls Nominatim + Open-Meteo APIs — specific integration (remove the GPT formatting layer, just return data) |
| `agents/fetch_articles.py` | **Keep as MCP tool** | Requires Neo4j credentials — fix the bug (return articles, don't just print them) |
| `agents/Neo4j_Article_Manager.py` | **Keep as MCP tool** | Requires Neo4j credentials |
| `agents/agent_logs.py` | **Keep as MCP tool** | The incremental position tracking is stateful behavior worth preserving (remove the GPT formatting layer, just return raw data) |
| `shared/state.py` | **Keep, simplify** | May still be useful for editor state or future stateful tools |
| `CLAUDE.md` | **Keep, update** | Project context for Claude Code's system prompt |

### What Gets Removed

| Component | Why |
|---|---|
| `agents/file_system.py` | Claude Code does this natively (Read, Write, Glob, Grep) |
| `agents/file_editor.py` | Claude Code edits files directly — no need for a browser editor |
| `agents/git_local_read.py` | Claude Code runs git commands natively |
| `agents/github_read.py` | Claude Code uses `gh` CLI natively |
| `agents/websearch_openai.py` | Claude Code has built-in WebSearch |
| `agents/large_tasks.py` | Claude Code decomposes tasks naturally — this was a workaround |
| `agents/maker.py` | Superseded by Claude Code SDK for code generation |
| `agents/agent_editor.py` | Claude Code edits files directly |
| `agents/agent_read.py` | Claude Code can read agent files and list tools via MCP introspection |
| `agents/delete_agent.py` | Claude Code can delete files and call discover_tools() |
| `agents/spark_to_bloom_updater.py` | Claude Code runs git commands — hardcoded path to another project is fragile |
| `agents/triage.py` | Empty file, no implementation |
| `agents/ollama.py` | Utility for Ollama — no longer needed as internal infrastructure |
| `agents/OpenAI Programming Assessor.py` | Dead code, broken legacy API |
| `agents/get_system_info.py` | Claude Code can run `uname`, `free`, `df`, `lscpu` etc. directly |
| `workflows/edit_agent.py` | 7-node LangGraph state machine — Claude Code edits files in one shot |
| `workflows/create_agent.py` | Moves into the core loop (see below) — no longer a separate "tool" |
| `workflows/models/feedback.py` | LangGraph workflow model — no longer needed |
| `models/maker.py` | Pydantic models for old code generation pipeline |
| `fastapi_server.py` | Old browser-based code editor — replaced by the new web app |
| `templates/edit.html` | Old editor template — replaced by the new web UI |
| `utilities/openai_tools.py` | GPT wrappers used by removed agents — Claude Code replaces these |
| `utilities/messages.py` | Chat history helpers used by removed agents |
| `utilities/open_web-ui.py` | Open Web-UI integration — separate concern |
| `models/open_web_ui.py` | Open Web-UI models — separate concern |
| `gradio_app.py` | Already replaced by terminal_app.py |

### What Gets Created

| Component | Purpose |
|---|---|
| `web_app.py` | **Web interface** — FastAPI + WebSocket server. Chat UI, voice via browser mic, settings panel. The production-ready interface for Docker. |
| `web/index.html` | **Web UI** — Chat interface with markdown rendering, voice controls, settings |
| `web/static/` | **Static assets** — JS, CSS for the web interface |
| `config.py` | **Configuration** — Centralized config with backend switching (Anthropic/Ollama), server addresses, ports |
| `agents/tool_creator.py` | **MCP tool** — Claude Code calls this to create new tools on the fly. Writes the file, calls `discover_tools()`, returns confirmation. |
| `agents/secret_manager.py` | **MCP tool** — Manages secrets for dynamically created tools. When a new tool needs an API key, prompts the user and stores it in `.env`. |

---

## Architecture Detail

### Request Flow

```
1. User types or speaks a request
2. terminal_app.py captures input (text or STT)
3. Input sent to Claude Code SDK with:
   - System prompt (personality, capabilities, tool creation instructions)
   - Chat history (for context)
   - MCP server connection (for external tools)
4. Claude Code processes the request:
   a. If it can handle it directly (file ops, git, code, reasoning) → does it
   b. If it needs external data → calls an MCP tool (weather, crypto, Neo4j, etc.)
   c. If no capability exists → creates a new MCP tool via tool_creator
5. Response streamed back to terminal_app.py
6. Output displayed as text and optionally spoken via TTS
```

### Self-Improving Capability

The key innovation of Hive Mind — creating new tools on the fly — is preserved but simplified:

**Before:** `create_agent` workflow → Claude Code SDK generates a full agent file → `discover_tools()`

**After:** Claude Code itself decides it needs a new tool, generates the code, and calls `tool_creator` to register it:

```python
# agents/tool_creator.py
@tool(tags=["system"])
def create_tool(file_name: str, code: str) -> str:
    """Write a new tool file to agents/ and register it.

    Claude Code generates the code and passes it here for registration.
    The code must use the @tool() decorator pattern.
    """
    file_path = os.path.join("agents", file_name)
    with open(file_path, "w") as f:
        f.write(code)
    discover_tools(["agents"])
    return f"Tool registered from {file_path}"


@tool(tags=["system"])
def install_dependency(package: str) -> str:
    """Install a Python package needed by a tool."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        capture_output=True, text=True
    )
    return result.stdout + result.stderr
```

When secrets are needed, Claude Code asks the user directly through the conversation (since it's a REPL) and uses `secret_manager` to persist them.

### MCP Integration

Claude Code connects to the MCP server to access external tools. The `services/claude_code.py` bridge needs to be updated to pass the MCP server URL:

```python
options = ClaudeAgentOptions(
    cwd=PROJECT_ROOT,
    system_prompt=system_prompt,
    permission_mode=permission_mode,
    mcp_servers=[{"url": "http://localhost:7777"}],  # or stdio
)
```

This means the MCP server needs to be running alongside the terminal app. The `start_all.sh` script handles this, or we could launch it as a subprocess from `terminal_app.py` on startup.

### Simplified Tool Pattern

Remaining MCP tools become pure data fetchers — no LLM formatting layer. Currently, tools like `get_weather_for_location` and `agent_logs` fetch data and then pipe it through GPT for "pretty" formatting. This is wasteful — Claude Code will format the response naturally. Tools should just return raw data:

```python
# Before (current)
@tool(tags=["weather"])
def get_weather_for_location(location, time_span, messages):
    data = fetch_weather_api(location)
    # 30 lines of GPT streaming to format the response
    for chunk in completions_streaming(f"Format this weather: {data}", ...):
        yield chunk

# After (proposed)
@tool(tags=["weather"])
def get_weather_for_location(location: str, days: int = 3) -> str:
    """Get weather forecast for a location. Returns raw weather data."""
    lat, lon = geocode(location)
    data = fetch_open_meteo(lat, lon, days)
    return json.dumps(data)
```

Claude Code receives the raw data and presents it naturally in conversation.

---

## Dependency Cleanup

### Remove
```
flask                       # Unused
langgraph                   # No more workflow state machines
langgraph-sdk               # No more workflow state machines
llama-index-llms-langchain  # Unused
graphviz                    # Unused
py2neo                      # Replace with neo4j driver (keep one)
psutil                      # get_system_info removed
GitPython                   # Claude Code runs git directly
bs4                         # Unused currently
```

### Keep
```
openai                  # STT/TTS in services/speech.py
python-dotenv           # .env loading
requests                # HTTP calls in remaining tools (coingecko, weather)
agent_tooling           # Tool discovery for MCP server
setuptools              # Standard
neo4j                   # Neo4j tools
mcp[cli]                # MCP server
mcpo                    # MCP proxy
uvicorn                 # Web app + MCP server
sounddevice             # Audio I/O (terminal voice mode)
numpy                   # Audio processing
pydub                   # MP3 decoding for TTS
keyboard                # Spacebar voice mode (terminal only)
claude-agent-sdk        # The CPU
pyfiglet                # Welcome banner (optional, nice touch)
```

### Add
```
fastapi                 # Web interface server (replaces old fastapi_server.py usage)
websockets              # WebSocket support for FastAPI
jinja2                  # Template rendering for web UI (already implicit dep)
```

---

## File Structure After Refactor

```
hive_mind/
├── agents/                         # MCP tools (external integrations only)
│   ├── coingecko.py               # Crypto prices (CoinGecko API)
│   ├── get_weather_for_location.py # Weather (Open-Meteo API) — simplified
│   ├── fetch_articles.py          # Neo4j article reader — fixed
│   ├── Neo4j_Article_Manager.py   # Neo4j article writer
│   ├── agent_logs.py              # System log scanner — simplified
│   ├── tool_creator.py            # NEW: Register new tools at runtime
│   ├── secret_manager.py          # NEW: Manage .env secrets for new tools
│   └── [dynamically created tools appear here]
├── services/
│   ├── speech.py                  # STT/TTS (unchanged)
│   └── claude_code.py             # Claude Code SDK bridge (enhanced)
├── web/                            # NEW: Web interface assets
│   ├── index.html                 # Chat UI
│   └── static/                    # JS, CSS
│       ├── app.js                 # WebSocket client, voice, markdown rendering
│       └── style.css              # Chat styling
├── shared/
│   └── state.py                   # Minimal state management
├── config.py                      # NEW: Centralized configuration
├── web_app.py                     # NEW: FastAPI + WebSocket server
├── terminal_app.py                # REPL shell + voice mode (simplified)
├── mcp_server.py                  # MCP server for tools (simplified)
├── start_all.sh                   # Launch MCP + web server (or terminal)
├── stop_all.sh                    # Stop services
├── requirements.txt               # Trimmed dependencies
├── .env                           # Backend config, API keys, server addresses
├── CLAUDE.md                      # Updated project context
├── Dockerfile                     # Updated for fewer deps
└── docker-compose.yml             # Web app + MCP server
```

~20 files removed. ~7 files created (web UI, config, web_app). Net reduction of ~13 files, but the new files are focused and purposeful.

---

## Migration Steps

### Phase 1: Configuration & Core Loop
1. Create `config.py` with backend switching (Anthropic/Ollama), server addresses, ports
2. Update `services/claude_code.py` to support chat history, MCP server connection, and read backend config from `config.py`
3. Rewrite `process_message()` to use Claude Code SDK with a callback-based `on_chunk` pattern (shared between terminal and web)
4. Remove all OpenAI/Ollama tooling imports, triage logic, and LangGraph workflow routing from `terminal_app.py`
5. Add `/backend` command to terminal for runtime backend switching
6. Test: basic text conversation works through Claude Code with both Anthropic and Ollama backends

### Phase 2: Tool Cleanup
1. Delete tools that Claude Code handles natively (file_system, git_local_read, github_read, websearch_openai, etc.)
2. Simplify remaining tools — strip GPT formatting layers, return raw data
3. Fix bugs in remaining tools (fetch_articles return value, agent_read duplicates)
4. Create `tool_creator.py` and `secret_manager.py`
5. Test: MCP server starts with only the remaining tools

### Phase 3: Web Interface
1. Create `web_app.py` — FastAPI + WebSocket server
2. Create `web/index.html` — chat UI with markdown rendering
3. Create `web/static/app.js` — WebSocket client, voice recording via MediaRecorder, audio playback
4. Create `web/static/style.css` — clean chat styling
5. Wire up voice: browser mic → WebSocket (base64) → server STT → process → TTS → WebSocket (audio) → browser playback
6. Add settings panel: backend toggle, model name, TTS voice, tool list
7. Test: full conversation flow in browser with streaming, voice, and settings

### Phase 4: Self-Improvement Loop
1. Update CLAUDE.md system prompt with tool creation instructions
2. Test the create-on-the-fly flow: ask for something no tool handles → Claude Code generates tool → tool_creator registers it → tool is immediately available
3. Test the secret flow: ask for something needing an API key → Claude Code asks user → secret_manager stores it → tool uses it

### Phase 5: Docker & Cleanup
1. Remove dead files (gradio_app.py, old fastapi_server.py, templates/, old workflows, old models)
2. Trim requirements.txt, add new deps (fastapi, websockets)
3. Update Dockerfile — web_app.py as the default entrypoint
4. Update docker-compose.yml — web app on port 7780, MCP server internal, mount `~/.claude` for credentials
5. Update CLAUDE.md with new architecture docs
6. Update start_all.sh / stop_all.sh for both modes (terminal vs web)
7. Final integration test: web UI chat, voice in browser, terminal fallback, MCP tools, tool creation, Ollama backend, Docker deployment

---

## What We Gain

1. **Dramatically less code to maintain** — ~20 files of brittle tool code deleted, replaced by Claude Code's native capabilities
2. **Better results** — Claude Code is smarter than frozen GPT-4o wrappers at file ops, git, web search, code generation
3. **No more triage fragility** — No more "GPT classified your message as the wrong tag" failures
4. **Real self-improvement** — Claude Code can reason about what tool to create, generate better code, test it, and iterate — all autonomously
5. **Simpler mental model** — UI shell → Claude Code brain → MCP peripherals. That's it.
6. **Fewer dependencies** — Drop langgraph, flask, GitPython, psutil, py2neo, bs4, llama-index
7. **Production-ready web interface** — Works in Docker, works on mobile, works from anywhere on the network. No terminal required.
8. **Backend flexibility** — Run against Anthropic for max capability, or Ollama for free/private/local operation. Switch with one config change.
9. **Two interface modes** — Terminal REPL for local dev, web UI for production/Docker/remote. Same backend, same tools, same voice capabilities.

## What We Lose

1. **Open Web-UI integration** — The utilities for Open Web-UI are removed. Could be a separate project if needed.
2. **Fine-grained tool selection** — The tag-based triage let you control exactly which tools were considered. Claude Code decides on its own. This is generally better but less controllable.
3. **Multi-model switching within a session** — The old `/model ollama qwen3:8b` then `/model openai gpt-4o` mid-conversation switching is replaced by a simpler backend toggle (Anthropic vs Ollama). You pick one backend per session rather than mixing models per-message.

---

## Open Questions

1. **MCP server startup**: Should `web_app.py` / `terminal_app.py` auto-launch the MCP server as a subprocess, or keep it as a separate process? Auto-launch is more convenient for Docker (single entrypoint); separate process is more debuggable during development.

2. **Chat history persistence**: Currently chat history is in-memory only. With a web app, should we persist conversations to disk or a lightweight DB (SQLite)? Cross-session memory is more valuable for a personal assistant — and the web UI could show conversation history in a sidebar.

3. **Claude Code SDK permissions**: The SDK has permission modes (default, acceptEdits, etc.). What should the default be? `acceptEdits` is more autonomous but riskier. `default` prompts for every file write. For a personal assistant, something permissive makes sense — but the web UI would need to surface permission prompts if using `default` mode.

4. **Docker credentials**: Claude Code SDK needs `~/.claude` credentials. Plan is to mount the host's `~/.claude` directory. When using Ollama backend, no Anthropic credentials are needed — just the Ollama server address.

5. **Web UI framework choice**: Vanilla JS keeps dependencies minimal and avoids build steps. But if the UI grows in complexity (conversation sidebar, file browser, settings), a lightweight framework (htmx + Alpine.js, or even React) might be worth it. Start vanilla, upgrade if needed?

6. **Ollama model capabilities**: Not all Ollama models support tool use equally. Should the system prompt adapt based on backend? e.g., when using Ollama, lean more heavily on MCP tools and be more explicit about available capabilities.
