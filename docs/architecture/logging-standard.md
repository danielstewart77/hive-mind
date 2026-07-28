# Unified logging standard

Every hive process logs through `hive_logging.py` — one JSON object per
record, written to stderr so Docker/systemd capture semantics are unchanged.
The module wraps the standard library instead of replacing it: existing
`logger.info(...)` calls keep working and land in the same JSON stream, while
`log_event()` adds the stable event names and structured fields needed to
trace what happened, who did it, and why it failed.

Copies of the module (build contexts cannot reach outside themselves):

- `core/hive_logging.py` — bots, harness backends, voice (imported as
  `core.hive_logging`)
- `nervous-system/hive_logging.py` — comms and lucent containers (imported as
  `hive_logging`; each Dockerfile COPYs it into the image root)

The edge-minds repo (`hive-edge-minds`) carries its own copy with the same
event vocabulary.

## Record shape

```json
{"timestamp":"2026-07-27T14:03:11.482Z","level":"INFO","service":"hive-comms",
 "logger":"hive-mind.sessions","message":"turn.started","event":"turn.started",
 "request_id":"6f9c…","session_id":"…","mind_id":"…","content_chars":142}
```

- `timestamp` — UTC, millisecond precision, `Z` suffix.
- `service` — `HIVE_SERVICE` env, falling back to `MIND_ID`, then logger name.
- `event` — present only on `log_event()` records; the searchable key.
- `exception` — `{type, message, stacktrace}` when `exc_info` is set.
- Context fields (e.g. `request_id`, `component`) merge in from the async
  contextvar set by the FastAPI middleware.

## API

- `configure_logging(service)` — call once at process start, replaces
  `logging.basicConfig`. Level from `HIVE_LOG_LEVEL` (default INFO). Quiets
  `httpx`/`httpcore`/`aiohttp.access` to WARNING.
- `log_event(logger, event, level=INFO, exc_info=None, **fields)` — emit a
  named event. Fail-open: a logging error never propagates into the
  operation being observed.
- `install_fastapi_logging(app, logger, component)` — middleware that emits
  `http.request.started` / `completed` / `failed` with `elapsed_ms` and
  status-derived level (5xx→ERROR, 4xx→WARNING), correlates via the
  `x-request-id` header (accepted inbound, always set on the response), and
  skips `/health`. Uvicorn runs with `log_config=None, access_log=False` so
  the middleware is the single source of request logs.

## Event vocabulary

Names are `noun.verb[.qualifier]`, past tense for completed facts. In use:

| Area | Events |
|---|---|
| Service lifecycle | `service.started`, `service.stopped`, `service.routes.registered` |
| HTTP | `http.request.started`, `http.request.completed`, `http.request.failed` |
| Sessions | `session.created`, `session.spawned`, `session.respawn.started`, `session.adopted`, `session.closed`, `session.pty.spawned` |
| Turns | `turn.started`, `turn.completed` |
| Broker | `broker.message.accepted`, `broker.wakeup.completed`, `broker.wakeup.timed_out`, `broker.wakeup.failed` |
| Mind registry | `mind.registered`, `mind.updated`, `mind.deleted` |
| Memory (lucent) | `memory.stored`, `memory.updated`, `memory.deleted` |
| Groups | `group_session.created` |
| Scheduler | `scheduled_skill.started`, `scheduled_skill.completed` |
| Surfaces | `surface.command.received` |
| Voice | `voice.stt.completed`, `voice.tts.completed` |

Attribution fields ride on every event that changes state: `mind_id` (never
the display name), `session_id`, `conversation_id`, `owner_type`/`owner_ref`,
`user_id`/`client_ref` for surface actions.

## Privacy and safety rules

1. **No message bodies.** User content, transcripts, and model responses are
   logged as `content_chars`/`transcript_chars`/`response_chars` counts only.
2. **Secrets are redacted structurally.** Any field whose key matches
   `authorization|cookie|credential|password|private_key|secret|token` is
   replaced with `[REDACTED]`, recursively.
3. **Bounded output.** Strings truncate at 4096 chars, lists at 100 items,
   nesting at depth 5.
4. **Fail-open.** `log_event` swallows its own errors; instrumentation must
   never change the success/failure semantics of the code path it observes.

## Retention

`docker-compose.example.yml` sets a shared `x-default-logging` anchor
(json-file, 20 MB × 5 files) applied to every service; systemd units inherit
journald's rotation.

## Instrumenting new code

Log an event when state changes or an external actor acts: a row is written,
a process is spawned or killed, a message crosses a boundary, a scheduled job
fires, a request mutates something. Do not log polling, reads, or progress
chatter at INFO. Reuse existing event names before minting new ones, and keep
the `noun.verb` shape.
