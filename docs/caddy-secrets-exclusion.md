# Caddy Configuration — Secrets Endpoint Exclusion

The `/secrets/*` endpoints must NOT be proxied by Caddy. They are internal-only,
reachable only on the Docker network.

## Current state

The gateway at port 8420 is proxied by Caddy under your domain.
The Caddyfile location depends on your deployment (configured via `HOST_CADDY_DIR`).

## Required change

Add a `respond` block before the reverse_proxy to block `/secrets/*` from external access:

```caddy
  handle /secrets/* {
    respond "Forbidden" 403
  }

  handle /api/* {
    reverse_proxy 203.0.113.10:8420
  }
```

Or, if using path-based routing, ensure `/secrets/*` is never included in the proxy rules.

## Why

The secrets endpoint (`GET /secrets/{key}`) returns plaintext secret values.
It uses Docker network identity (source IP → container name) to authenticate
callers. External requests bypass this — they come from Caddy's IP, not a
mind container. Blocking at Caddy ensures the endpoint is only reachable
from within the Docker network.
