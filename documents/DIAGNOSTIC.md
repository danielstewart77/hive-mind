# System Diagnostic: Telegram Bot Outage

**Date:** 2026-03-02
**Branch:** `refactor/claude-code-cpu`
**Duration:** ~2 hours (approx 23:58 - 01:52 UTC)
**Reported symptom:** Telegram bot not responding to messages

---

## Root Cause Chain

Four independent failures compounded into a full outage. Each was necessary but not sufficient on its own.

### 1. Voice server crash-loop (docker-compose.yml)

`read_only: true` on the voice-server container with no writable cache volume. Whisper model download fails on every startup:

```
OSError: [Errno 30] Read-only file system: '/home/hivemind/.cache'
```

**Impact:** Voice messages fail (STT DNS resolution error to crash-looping container). Text path unaffected by this alone.

**Fix:** Added `whisper-cache` named volume mounted at `/home/hivemind/.cache`. Added `.cache` directory creation to Dockerfile so the volume inherits correct ownership (UID 1000/hivemind).

### 2. Missing `openai` dependency (requirements.txt)

`agent_tooling` imports `openai` at the top level (`from openai import OpenAI`). `openai` was not in `requirements.txt` — it existed in the container only as a cached Docker build layer.

When the Dockerfile was modified (to add `.cache` dir for fix #1), the build cache was invalidated from the `usermod` step onward, triggering a full venv rebuild. The new venv lacked `openai`.

```
ModuleNotFoundError: No module named 'openai'
```

**Impact:** The stdio MCP server (`mcp_server.py`) crashes immediately on import. Claude Code spawns but hangs waiting for the dead MCP server to initialize. No response is ever produced.

**Fix:** Added `openai` to `requirements.txt`. Forced `docker compose build --no-cache server`.

### 3. Read-only home directory blocking Claude Code config (docker-compose.yml)

`read_only: true` on the server container prevents Claude Code from writing `/home/hivemind/.claude.json` (per-user config). The `.claude/` subdirectory was writable (bind mount), but the parent `/home/hivemind/` was on the read-only root filesystem.

```
EROFS: read-only file system, open '/home/hivemind/.claude.json'
```

Claude Code logs the error and stalls — no child processes spawned (no MCP servers, no API calls). The process stays alive (sleeping) but never produces output.

**Impact:** Claude process appears running but is inert. Gateway SSE stream stays open indefinitely. Telegram bot shows "..." forever.

**Fix:** Added `/home/hivemind` as a tmpfs mount in the server container. Docker correctly layers the `.claude` bind mount on top, so credentials remain accessible.

```yaml
tmpfs:
  - /tmp
  - /home/hivemind:uid=1000,gid=1000
```

### 4. Session reaping during active processing (core/sessions.py)

`last_active` was only updated on message send and on the final `result` event. During long-running operations (HITL approval waits, docker builds, tool calls), the session appeared idle after 30 minutes and was killed by the reaper.

**Impact:** The original HITL timeout cascade described in the bug report. Active sessions killed mid-processing, SSE stream terminates, telegram bot shows partial or no response.

**Fix:** Updated `send_message` to set `last_active` on every yielded event from the Claude process, not just on send and result.

---

## Remediation Steps (chronological)

1. Read all modified files (`hitl.py`, `server.py`, `telegram_bot.py`, `gateway_client.py`, `sessions.py`) and verified Ada's uncommitted changes were syntactically correct and logically sound.

2. Diagnosed voice server crash-loop from `docker compose logs voice-server`. Added `whisper-cache` volume to `docker-compose.yml` and `.cache` directory to Dockerfile. Deleted root-owned volume, rebuilt. Voice server started successfully and downloaded Whisper model.

3. Telegram bot was polling but not processing messages. Traced message flow: bot received message, sent "..." placeholder, server spawned Claude process, but Claude produced no output for 7+ minutes.

4. Checked Claude debug logs inside container — found `EROFS` errors on `.claude.json` write, then silence. Checked process tree — Claude had zero child processes (no MCP servers started).

5. Tested MCP server import inside container — discovered `ModuleNotFoundError: No module named 'openai'`. Added `openai` to `requirements.txt`, forced no-cache rebuild.

6. After rebuild, Claude still hung. Debug logs showed same EROFS pattern. Added tmpfs at `/home/hivemind` to server container so Claude Code can write `.claude.json`.

7. Restarted server. Telegram bot's in-flight request failed (expected — server restarted mid-stream). User sent new message — response streamed successfully with live `editMessageText` updates.

---

## Pre-existing Changes Validated (Ada's uncommitted work)

These changes were already in the working tree and confirmed correct:

- **Variable HITL TTL** (`core/hitl.py`, `server.py`): `DEFAULT_TTL = 180`, per-request TTL clamped 30s-10min. HITL endpoint accepts `ttl` parameter.
- **Unlimited aiohttp timeout** (`telegram_bot.py:586`): `ClientTimeout(total=0, sock_read=0)` on the Telegram bot's HTTP session.
- **HITL approval UX** (`telegram_bot.py:506`): Changed "Approved." to "Approved. Operation in progress, please wait..."
- **Image support** (`gateway_client.py`, `server.py`, `sessions.py`, `telegram_bot.py`): Photo handler, multimodal message content, base64 image passthrough.
- **Voice streaming** (`telegram_bot.py`): Replaced blocking query+TTS with progressive streaming text + voice-at-end via `_stream_to_message(voice=True)`.

---

## Remaining Work

- **MCP compose tools**: The hive-mind-mcp container's compose tools should pass `"ttl": 600` in their `/hitl/request` POST body to use the longer TTL for docker operations.
- **Commit**: All changes are uncommitted on `refactor/claude-code-cpu`. Files modified: `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `core/hitl.py`, `core/sessions.py`, `core/gateway_client.py`, `server.py`, `clients/telegram_bot.py`.
