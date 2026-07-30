# Unified Hive Logging Standard

## Purpose

Every Hive service emits the same machine-searchable record shape so an
operator can reconstruct failures and consequential user or Mind actions across
service boundaries. Logging is diagnostic and audit evidence, not application
state: a logging failure must never change an operation's result.

## Rollout plan

1. Put the standard formatter, redaction, correlation, and FastAPI middleware
   in the central control plane, canonical Edge Mind runtime, Hive Tools, and
   deployed outposts.
2. Cover every HTTP mutation automatically, including duration and status, then
   add domain events for actions whose meaning is not captured by a route.
3. Cover uncaught exceptions, caught/retried failures, subprocess stderr and
   exits, startup/shutdown failures, and external-service failures.
4. Add actor, Mind, session, conversation, and request identifiers wherever
   they are known. Propagate `X-Request-ID` between services.
5. Keep prompts, response bodies, message text, credentials, tokens, cookies,
   and personal content out of INFO logs. Record sizes, counts, types, IDs, and
   outcomes instead.
6. Emit to stderr/stdout for the platform collector. Rotate Docker JSON logs at
   `20m` x 5 files per service; use equivalent bounded retention on systemd and
   Windows deployments.
7. Validate formatter/redaction behavior, representative mutation events,
   exception traces, request correlation, existing tests, and container output.

## Record contract

Production output is newline-delimited JSON. Each record contains:

| Field | Requirement |
|---|---|
| `timestamp` | UTC RFC 3339 with milliseconds |
| `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `service` | Stable deployment/service name |
| `logger` | Python logger/component name |
| `message` | Human-readable summary; normally the event name |
| `event` | Stable dotted event name for domain and audit records |
| `request_id` | Correlation ID for HTTP work, when applicable |
| domain IDs | `session_id`, `mind_id`, `conversation_id`, `user_id`, etc. when known |
| `elapsed_ms` | Duration on requests and long-running operations |
| `exception` | Type, message, and stack trace for exceptions |

`HIVE_LOG_LEVEL` controls severity (default `INFO`). `HIVE_LOG_FORMAT=text` is
an Edge Mind operator escape hatch; JSON remains the default.

## Levels

- `DEBUG`: read-only request traffic, polling, diagnostic details, retries that
  are fully expected, and high-volume internals.
- `INFO`: successful state changes, accepted user/Mind actions, lifecycle
  transitions, and mutating request entry/exit.
- `WARNING`: rejected authentication, invalid/conflicting requests, incomplete
  turns, non-zero subprocess exits, degraded fallbacks, and recoverable failures.
- `ERROR`: failed operations, timeouts, external-service failure, subprocess
  crashes, and caught exceptions that prevent the requested outcome.
- `CRITICAL`: process-level startup/runtime failure.

## Event naming and required action coverage

Events use `noun[.noun].past-tense` (for example `session.created`,
`rule.updated`, `mind.restart.requested`). Request envelopes use
`http.request.started|completed|failed`.

The following state changes require an INFO-or-higher domain event:

- authentication success/rejection and logout;
- Mind bootstrap, registration, configuration, restart, and voice reference;
- session create, reuse, interrupt, release, rotation, and closure;
- turn start, completion, rejection, incomplete termination, and failure;
- chat-surface commands and accepted text/photo/voice messages (metadata only);
- rules/memory create, update, and delete;
- capability configuration and model pull/delete;
- HITL decision and replay scheduling;
- Hive Tools mutation routes and tool failures;
- remote-admin SSH connect/close, command outcome, and shell attach/detach;
- canvas mutations and review dispatch;
- voice STT/TTS outcome and wake-word configuration;
- service startup, shutdown, and fatal failure.

## Privacy and safety

The formatter recursively redacts keys containing authorization, cookie,
credential, password, private key, secret, or token. Values are depth-, count-,
and length-bounded before JSON encoding.

Never log:

- authorization headers, session cookies, access tokens, passwords, or private keys;
- prompt/message/email/SMS content, model responses, transcripts, canvas text, or command text;
- raw request/response bodies or uploaded media.

For sensitive actions, log safe metadata: character/byte counts, object IDs,
target service, outcome, and elapsed time. Remote command text is represented by
a short SHA-256 fingerprint plus its length.

## Correlation and research

Inbound HTTP requests accept a printable `X-Request-ID` up to 128 characters or
mint a UUID. The ID is returned in the response and attached through an async
context variable to all records emitted while the request is active. Outbound
clients should forward that ID; domain IDs allow correlation when a stream
outlives its initial HTTP response.

Examples:

```bash
docker compose logs --no-color | jq -R 'fromjson? | select(.level == "ERROR")'
docker compose logs --no-color | jq -R 'fromjson? | select(.session_id == "SESSION")'
docker compose logs --no-color | jq -R 'fromjson? | select(.event == "rule.deleted")'
docker compose logs --no-color | jq -R 'fromjson? | select(.request_id == "REQUEST")'
```

## Compatibility invariants

- Existing standard-library logging calls continue to work and receive the JSON
  formatter even before they gain a domain event.
- Logging is fail-open and must not raise into application code.
- HTTP response bodies, status codes, SSE framing, and WebSocket protocols do
  not change. The only protocol addition is the `X-Request-ID` response header.
- Uvicorn access logs are disabled where middleware provides the correlated
  equivalent, preventing duplicate/noisy records.
- Stateless CLI tools retain stdout as their JSON result channel; diagnostic
  logging belongs on stderr.

## Repository coverage

| Component | Source of truth | Required integration |
|---|---|---|
| Hive control plane | `hive` | middleware, actor-aware domain events, rotation |
| Edge Mind runtime | `hive-edge-minds` | session/turn/surface/voice/admin events, rotation |
| Hive Tools | `hive-tools` | middleware, HITL/auth events, existing tool exceptions |
| Deployed outposts | per-Mind checkout | receive canonical Edge Mind logging contract |
| Hive Comms/Lucent | deployment source when available | same formatter, request propagation, broker/session/memory events |

Completion requires evidence from every row; an unavailable repository or
runtime is recorded as remaining work, not silently treated as covered.
