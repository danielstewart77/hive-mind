# SMS inbound webhook

The nervous system terminates the inbound side of the SMS gateway: a `POST /sms/inbound` endpoint that the phone-side `sms-gate.app` calls when a message arrives. The outbound send half lives in hive-tools — see `docs/sms-gateway.md` there.

## Path

```
Phone receives SMS
  → sms-gate.app POSTs webhook to https://sms.sparktobloom.com/sms/inbound
    → Cloudflare → Caddy (path-routed) → hive-comms:8426
      → verify HMAC → extract fields → dispatch to Ada
```

Caddy routes `/sms/*` on `sms.sparktobloom.com` to hive-comms. The hostname is shared with the outbound gateway, which sits at `/api/*` and `/health/*` (routed to the `sms-gateway` container instead).

## Code

`comms/sms_inbound.py` holds the pure helpers — no FastAPI imports, no I/O:

- `verify_signature(body, timestamp, signature, secret)` — HMAC-SHA256 of `raw_body + timestamp_string` against `SMS_INBOUND_HMAC_SECRET`. Constant-time compare against the `X-Signature` header (lowercase hex). The timestamp comes in `X-Timestamp` as unix seconds.
- `extract_message_fields(payload)` — permissive field extraction. The gateway nests event fields under `payload`, but the function falls back to flat shapes for testability. Returns `{sender, text, message_id, received_at, event, device_id}`.
- `format_dispatch_content(sender, text)` — formats the broker message string Ada (or any recipient) sees.
- `build_broker_message_id(sender, gateway_message_id)` — deterministic dedup key for the broker. The sms-gate client retries non-2xx deliveries, so the broker needs idempotency; key shape is `sms-{sender}-{gateway_message_id}`, with a UUID fallback when either side is missing.

The route module wiring those helpers into a FastAPI endpoint lives alongside the other comms routes.

## Toggle

`/sms/inbound/enabled` (GET/PUT) flips the listener on or off. Hive-tools proxies this through to surface the toggle in the Hive Tools UI without exposing hive-comms publicly.

## Env vars

Set in `/home/daniel/Storage/Dev/hive_mind/nervous-system/.env` (mode 600):

- `SMS_INBOUND_HMAC_SECRET` — 32-byte random, used by `verify_signature`.

## Security

- HMAC verification is mandatory on every inbound. Without it the public-facing endpoint would accept arbitrary "SMS" injected into Ada's context.
- The HMAC body is `raw_body + timestamp_string`. Timestamp window enforcement (reject far-future / far-past) lives in the route module, not in the pure helpers.
- SMS content is treated as untrusted input. Ada drafts replies but cannot send without the outbound side's HITL approval (see hive-tools `docs/sms-gateway.md`).
- Dedup by `build_broker_message_id` prevents retry storms from re-dispatching the same SMS multiple times.

## Permissive payload shape

The public `sms-gate.app` docs are stale on the `mms:downloaded` event. `extract_message_fields` accepts both the documented `sms:received` shape and a set of plausible aliases (`phoneNumber|sender|from`, `message|text|body|contentPreview`, etc.), so the route keeps working when the gateway adds fields or renames them between releases.
