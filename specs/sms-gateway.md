# SMS Gateway Integration

## Overview

Enables Ada to read incoming SMS messages and send replies on Daniel's behalf, using an Android SMS gateway app exposed over WireGuard + Cloudflare.

## Architecture

```
SMS arrives on Daniel's Android phone
  → sms-gate.app fires webhook POST to https://<subdomain>.domain.com/sms/inbound
    → Cloudflare → home server → Hive Mind server (new /sms/inbound endpoint)
      → Ada processes and optionally replies
        → Reply via sms-gate REST API (reachable over WireGuard when phone is connected)
```

## Components

### On Daniel's phone
- **App:** [sms-gate.app](https://sms-gate.app) — open source Android SMS gateway
- **Mode:** Local REST API only (no cloud relay) — all data stays self-hosted
- **Config:** Webhook URL set to `https://<subdomain>/sms/inbound`

### Cloudflare / DNS
- New subdomain added to existing Cloudflare DNS (same setup as Spark to Bloom)
- Routes port 443 inbound to Hive Mind server on home LAN
- nginx/Caddy on home machine adds a `location /sms/` proxy block pointing to `http://server:8420`

### Hive Mind server (new endpoints needed)
- `POST /sms/inbound` — receives webhook from sms-gate.app; authenticates via shared secret; queues message for processing
- `POST /sms/send` — internal endpoint Ada calls to send a reply via sms-gate REST API

### sms-gate API (outbound replies)
- `POST http://<phone-wireguard-ip>:<port>/v1/message` — sends SMS
- Phone must be on WireGuard for Hive Mind to reach it for replies
- Inbound webhooks work regardless of WireGuard state (phone pushes out)

## Security

- Webhook endpoint authenticated with a shared secret stored in keyring as `SMS_GATEWAY_SECRET`
- Inbound payloads treated as untrusted; no tool calls or code execution from SMS content
- Reply requires Ada judgment or explicit Daniel instruction — no fully automatic reply without approval policy defined

## Setup Steps (Daniel)

1. Install sms-gate.app on Android
2. Add Cloudflare DNS subdomain → home server port 443
3. Add nginx/Caddy location block for `/sms/` → `http://server:8420`
4. Configure sms-gate.app webhook URL + shared secret
5. Store `SMS_GATEWAY_SECRET` in keyring: `python3 -m keyring set hive-mind SMS_GATEWAY_SECRET`
6. Store `SMS_GATEWAY_URL` (phone's WireGuard IP + port) in keyring

## Setup Steps (Ada / Hive Mind)

1. Add `POST /sms/inbound` endpoint to `server.py`
2. Add `POST /sms/send` endpoint to `server.py` (proxies to sms-gate REST API)
3. Add SMS processing skill or extend existing `communication` skill routing
4. Define auto-reply policy with Daniel before enabling automatic responses

## Anti-patterns

- Do NOT use sms-gate.app cloud relay mode — SMS content would leave the home network
- Do NOT enable auto-reply without an explicit approval policy — SMS is a higher-trust channel than email
- Do NOT expose the sms-gate REST API directly to the internet — it has no auth; keep it LAN/WireGuard only
