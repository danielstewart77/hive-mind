# SMS gateway — outbound send + container deployment

Hive-tools owns the outbound half of the SMS path: a HITL-gated `/sms/send` endpoint, the self-hosted `sms-gateway` container that talks to the phone app, and the MariaDB backing store. Inbound webhooks land in hive_nervous_system — see `docs/sms-inbound.md` there.

## Components in this repo

### `tools/sms.py` — `/sms` router

- `POST /sms/send` (HITL-gated via `Depends(hitl_gate("sms_send"))`). On approve, POSTs `{message, phoneNumbers}` to the local sms-gateway's `/api/3rdparty/v1/messages` using basic auth.
- `GET /sms/inbound/enabled` and `PUT /sms/inbound/enabled` — thin proxies over hive-comms' inbound-listener toggle, so the toggle is reachable from the Hive Tools UI without exposing hive-comms directly.

### Containers (defined in `docker-compose.yml`)

- `sms-gateway` — `capcom6/sms-gateway:latest`, run in `private` mode. No host port published; reached only from `hive-tools` over the `sms-private` Docker network.
- `sms-gateway-db` — MariaDB backing store for `sms-gateway`. Same `sms-private` network, no host port.
- `hive-tools` itself attaches to both `hivemind` (so other minds reach it normally) and `sms-private` (so it alone can reach `sms-gateway`).

### Caddy routing (lives in `your Caddyfile`)

The public hostname `sms.example.com` is path-routed:

| Path prefix | Upstream | Purpose |
|---|---|---|
| `/sms/*` | `hive-comms:8426` | Inbound webhook from sms-gate.app → NS |
| `/api/*` | `sms-gateway:3000` | Phone app + 3rd-party API to the local gateway |
| `/health/*` | `sms-gateway:3000` | Gateway liveness/readiness |

Caddy attaches to the `sms-private` network so it can resolve `sms-gateway` by name.

## Env vars

Set in ``.env` at the repo root` (mode 600):

- `SMS_GATEWAY_URL` — base URL for the gateway container (e.g. `http://sms-gateway:3000`).
- `SMS_GATEWAY_USER` / `SMS_GATEWAY_PASS` — basic-auth credentials minted when the phone registered as a device.
- `SMS_GATEWAY_PRIVATE_TOKEN` — server-side access token for private-mode device registration.
- `SMS_GATEWAY_JWT_SECRET` — JWT signing secret for the gateway (≥32 chars).
- `SMS_GATEWAY_DB_ROOT_PASSWORD` / `SMS_GATEWAY_DB_PASSWORD` — MariaDB credentials.

## HITL behaviour

`hitl_gate("sms_send")` is the only gate on the route. New tool names auto-register into `tool_hitl_settings` at server startup (`sync_tool_registry` in `hitl.py`); no manual seed step. Default mode for a freshly-registered tool is `on` (always prompt).

Approval prompts surface in the owner's Telegram chat with destination and body preview before any send. On reject, the request raises `HITLPendingException` and the gateway is never contacted.

## Allowlist (scaffold)

A `sms_allowlist` table (`phone_e164 TEXT PRIMARY KEY, added_at INTEGER, note TEXT`) sits in front of the gate. If a destination is present, the handler skips HITL for that call. Ships empty; entries are deliberate per-destination decisions, never auto-added from approval history.

CRUD endpoints: `GET /sms/allowlist`, `POST /sms/allowlist`, `DELETE /sms/allowlist/{phone}`.

## Network isolation

Only `hive-tools`, `sms-gateway`, `sms-gateway-db`, and `caddy` are on `sms-private`. The ~19 other containers on `hivemind` cannot reach the gateway and cannot bypass HITL by POSTing directly.

## Security

- No host port published for the gateway. Container-to-container over `sms-private` only.
- Phone-app traffic terminates at Caddy, which proxies to `sms-gateway` over the same private network. Gateway authenticates the phone via the private-mode access token + JWT.
- SMS content is treated as untrusted input. The Ada-side skill drafts replies only; no tool calls fire from message content without HITL approval or an allowlist hit.
- FCM push notifications (wake-up only, no content) flow through `api.sms-gate.app`. No phone numbers and no message body leave home infrastructure.

## Phone-app side

- App: `capcom6/android-sms-gateway` (sms-gate.app), private-server mode.
- Registered as a private-server device; the user id and device id are issued at registration and live in `.env`.
- Bootstrap path for re-registration: POST `/api/mobile/v1/device` with `Authorization: Bearer $SMS_GATEWAY_PRIVATE_TOKEN` to mint user creds; `curl -u user:pass /api/mobile/v1/user/code` to mint a 6-digit OTP (5-min window); paste the OTP into the phone's "key" field.
- Grant `SEND_SMS` + `RECEIVE_SMS` only. Skip `READ_SMS` — the webhook fires on receive without it.

## Side project — HITL auto-registration

The `_REGISTERED_TOOLS` set in `hitl.py` is populated as a side effect of each `hitl_gate(name)` construction during route-module import. `sync_tool_registry(db)` runs in the FastAPI startup hook and `INSERT OR IGNORE`s any discovered name into `tool_hitl_settings` with the default mode. Existing user-edited rows are preserved.

Properties:
- Adding a new gated tool is one decorator line on the route — no manual seed.
- Renamed tools leave the old row as an orphan; orphans do nothing until manually cleaned.
- No route-introspection magic — the set lives in module-import space.
