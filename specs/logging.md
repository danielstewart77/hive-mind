# Logging Spec — Hive Mind Gateway (hive-comms)

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

- [x] `docker compose logs hive-comms` shows NO `httpx` INFO lines for Telegram polling
- [x] Every `POST /sessions/{id}/message` request produces at least one `[INFO]` line on entry
- [x] When a session is dispatched or respawned to a mind container, an `[INFO]` line appears with session ID, mind, and model
- [x] When a response takes >30 s, a `[WARNING]` line appears with elapsed time
- [ ] When `forward_to_mind` times out, an `[ERROR]` line appears with mind ID and elapsed time — `forward_to_mind` doesn't exist in the current gateway (it lived in the deleted `server.py`); this criterion needs re-scoping to the current dispatch path if it's still wanted
- [x] When a mind's subprocess emits stderr, it appears in that mind's own logs at `[WARNING]` (each mind's harness module drains its own subprocess stderr — the gateway itself has no subprocess to watch)
- [ ] Log rotation is configured: logs cap at ~100 MB (`max-size: 20m`, `max-file: 5`) — still open; no `logging:` block exists on any service in `docker-compose.example.yml` today
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

Already done — `nervous-system/comms/server.py` silences it alongside its `basicConfig` call.

---

## Gateway — `nervous-system/comms/server.py`

`POST /sessions/{session_id}/message` logs receipt (`message: session=... chars=...`) and completion with timing (`message: done session=... elapsed=...s`) around the SSE stream.

## Sessions — `nervous-system/comms/sessions.py`

This process dispatches to each mind's own container over HTTP — it does not spawn a Claude/Codex subprocess itself. It logs dispatch/respawn (`send_message: respawn session=... mind=... model=...`), the actual per-mind dispatch (`Spawned %s session %s via %s`), results with timing, and a `WARNING` when a response exceeds 30s. Each mind's own harness module (`minds/harness/claude_cli.py` / `codex_cli.py`) drains and logs its own subprocess stderr at `WARNING` — that's not visible in the gateway's own logs, only in that mind's container logs.

---

## Files to Touch

| File | Change |
|------|--------|
| `nervous-system/comms/server.py` | Done — httpx silenced, request entry/exit logged |
| `nervous-system/comms/sessions.py` | Done — dispatch, respawn, send start, result, slow-response warning all logged |
| `docker-compose.example.yml` | Open — add log rotation config to each service |

---

## Remaining Work

Only log rotation is still open: add a `logging:` block (`json-file` driver, `max-size: 20m`, `max-file: 5`) to each service in `docker-compose.example.yml`, then verify against the acceptance criteria above.
