# Logging Spec — Hive Mind Gateway

## User Requirements

As an operator, when a session or subprocess fails (timeout, crash, hang), I want to open
the container logs and immediately understand:
- Which session was affected, and which mind
- Whether the subprocess was spawned
- How long the operation ran before failing
- Whether the failure was on the gateway side or the subprocess side

I do not want to scroll through hundreds of lines of Telegram polling noise to find that signal.

---

## User Acceptance Criteria

- [ ] `docker compose logs hive_mind` shows NO `httpx` INFO lines for Telegram polling
- [ ] Every `POST /sessions/{id}/message` request produces at least one `[INFO]` line on entry
- [ ] When a session subprocess is spawned or respawned, an `[INFO]` line appears with session ID, mind, and model
- [ ] When a response takes >30 s, a `[WARNING]` line appears with elapsed time
- [ ] When `forward_to_mind` times out, an `[ERROR]` line appears with mind ID and elapsed time
- [ ] When a subprocess emits stderr, it appears in logs at `[WARNING]`
- [ ] Log rotation is configured: logs cap at ~100 MB (`max-size: 20m`, `max-file: 5`)
- [ ] Simulating the Bob timeout scenario produces the expected 6-line trace (see spec below)

---

## Goal

Give operators (and Ada) enough signal to diagnose failures without drowning in noise.
The Bob-session timeout on 2026-03-31 was undiagnosable because the gateway emitted zero
log lines during the entire incident. This spec closes that gap.

---

## Log Levels — What Goes Where

| Level     | What belongs here |
|-----------|-------------------|
| `DEBUG`   | Message content, subprocess stdout/stderr, token-level SSE events |
| `INFO`    | Request entry/exit with timing, session lifecycle events (spawn, respawn, kill, timeout), group chat routing |
| `WARNING` | Slow responses (>30 s), unexpected respawns, unknown mind fallbacks |
| `ERROR`   | Timeouts, subprocess crashes, failed HTTP requests, unhandled exceptions |

Default runtime level: **INFO**. `DEBUG` is off by default; enable per-session or via env var.

---

## Silence the Noise First

`httpx` currently logs every Telegram `getUpdates` poll at `INFO` — 864 lines/day, zero signal.

```python
logging.getLogger("httpx").setLevel(logging.WARNING)
```

Add this to `server.py` startup, alongside the existing `basicConfig` call.

---

## Gateway — `server.py`

### Message endpoint (`POST /sessions/{id}/message`)

Log request receipt and completion with timing:

```python
@app.post("/sessions/{session_id}/message")
async def send_message(session_id: str, body: MessageRequest):
    log.info("message: session=%s chars=%d", session_id, len(body.content))
    t0 = time.monotonic()

    async def event_stream():
        async for event in session_mgr.send_message(session_id, body.content, images=images):
            yield f"data: {json.dumps(event)}\n\n"
        log.info("message: done session=%s elapsed=%.1fs", session_id, time.monotonic() - t0)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

---

## Sessions — `core/sessions.py`

### `send_message()`

```python
log.info("send_message: start session=%s mind=%s", session_id, mind_id)

# Before spawn:
if needs_respawn:
    log.info("send_message: spawning session=%s mind=%s model=%s", session_id, mind_id, session["model"])

# On result or timeout:
log.info("send_message: result session=%s elapsed=%.1fs", session_id, elapsed)
log.warning("send_message: slow response session=%s mind=%s elapsed=%.1fs", session_id, mind_id, elapsed)  # if > 30s
```

### `_spawn()`

Add at entry and on process exit:

```python
log.info("spawn: session=%s mind=%s model=%s pid=%s", session_id, mind_id, model, proc.pid)
log.warning("spawn: process exited unexpectedly session=%s returncode=%s", session_id, proc.returncode)
```

### CLI path — subprocess stdout/stderr

Capture subprocess stderr at `WARNING`; stdout token events at `DEBUG`:

```python
log.debug("subprocess stdout: session=%s line=%s", session_id, line[:200])
# stderr:
log.warning("subprocess stderr: session=%s line=%s", session_id, err_line)
```

---

## Group Chat — `tools/stateful/group_chat.py`

### `forward_to_mind()`

```python
logger.info("forward_to_mind: start mind=%s group=%s", mind_id, group_session_id)
# after session lookup:
logger.info("forward_to_mind: using session=%s mind=%s", child_session_id, mind_id)
# on completion:
logger.info("forward_to_mind: done mind=%s elapsed=%.1fs", mind_id, elapsed)
# on timeout (already in except):
logger.error("forward_to_mind: timeout mind=%s after %.1fs", mind_id, elapsed)
```

---

## Log Rotation

Configure in `docker-compose.yml` for the `hivemind` service:

```yaml
logging:
  driver: "json-file"
  options:
    max-size: "20m"
    max-file: "5"
```

This caps log storage at ~100 MB total. Sufficient for months of normal operation.

---

## What This Would Have Shown During the Bob Timeout

```
[INFO]  forward_to_mind: start mind=bob group=44b2bf73
[INFO]  forward_to_mind: using session=618ddc0a mind=bob
[INFO]  message: session=618ddc0a chars=312
[INFO]  send_message: start session=618ddc0a mind=bob
[INFO]  send_message: spawning session=618ddc0a mind=bob model=gpt-oss:20b-32k
[WARNING] send_message: slow response session=618ddc0a mind=bob elapsed=90.0s
[ERROR]  forward_to_mind: timeout mind=bob after 120.0s
```

Six lines. Immediately tells you the subprocess was spawned but never returned.

---

## Files to Touch

| File | Change |
|------|--------|
| `server.py` | Silence httpx; add request entry/exit logging to message endpoint |
| `core/sessions.py` | Log spawn, respawn, send start, result, slow-response warning |
| `tools/stateful/group_chat.py` | Log forward_to_mind start, session lookup, completion, timeout |
| `docker-compose.yml` | Add log rotation config to hivemind service |

---

## Implementation Order

1. `server.py` — silence `httpx` at INFO; add entry/exit logs to `send_message` endpoint
2. `core/sessions.py` — add `send_message` start log, spawn/respawn logs, slow-response warning
3. `tools/stateful/group_chat.py` — add start, session-found, completion, and timeout logs with elapsed timing
4. `docker-compose.yml` — add `logging` block with `json-file` driver and rotation options
5. Run existing tests to confirm no regressions; verify log output manually against acceptance criteria
