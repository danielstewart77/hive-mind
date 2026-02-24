# Docker Host Access Plan

## Goal

Allow Claude (running inside the container) to manage the Hive Mind Docker stack — restart services, rebuild images, bring the stack up/down — without giving the container access to the Docker socket.

## Why Not Mount the Docker Socket?

Mounting `/var/run/docker.sock` into the container gives **full root-level host access**. Even with MCP tool allowlists, Claude's built-in `Bash` tool can run arbitrary `docker` commands — including mounting the host filesystem into a new container. This completely negates the sandbox.

## Approach: Host Webhook Service

A minimal HTTP server runs **on the host** (outside Docker), listening on a Unix socket or loopback port. It accepts a fixed set of commands and executes them as `docker compose` operations. The container hits this webhook via a mapped port or forwarded socket.

### Architecture

```
┌─────────────────────────────────────────────┐
│  Docker Container (hive-mind-server)        │
│                                             │
│  Claude CLI → Bash tool or MCP tool         │
│       ↓                                     │
│  curl http://host.docker.internal:9420/...  │
└─────────────┬───────────────────────────────┘
              │  HTTP (host.docker.internal)
┌─────────────▼───────────────────────────────┐
│  Host: hive-mind-webhook (systemd service)  │
│                                             │
│  Allowlist:                                 │
│    POST /restart/{service}                  │
│    POST /rebuild/{service}                  │
│    POST /up                                 │
│    POST /down                               │
│    GET  /status                             │
│                                             │
│  Executes: docker compose <command>         │
│  CWD: /home/USER/Storage/Dev/hive_mind    │
└─────────────────────────────────────────────┘
```

### Allowed Commands

| Endpoint | Action | docker compose equivalent |
|----------|--------|--------------------------|
| `GET /status` | Stack health check | `docker compose ps --format json` |
| `POST /restart/{service}` | Restart a service | `docker compose restart {service}` |
| `POST /rebuild/{service}` | Rebuild and restart | `docker compose up -d --build {service}` |
| `POST /up` | Start the full stack | `docker compose up -d` |
| `POST /down` | Stop the full stack | `docker compose down` |
| `GET /logs/{service}` | Tail recent logs | `docker compose logs --tail 50 {service}` |

Service names are validated against an allowlist: `["server", "discord-bot"]`.

### Implementation

#### 1. `hive_mind_webhook.py` (runs on host, NOT in container)

- Python script using `http.server` or FastAPI (minimal deps)
- Binds to `127.0.0.1:9420` (loopback only — not exposed to network)
- Validates service names against allowlist
- Runs `docker compose` via `subprocess.run()` with `cwd` set to the project dir
- Shared secret via `Authorization: Bearer <token>` header (token from `.env`)
- Returns JSON: `{"ok": true, "output": "..."}` or `{"ok": false, "error": "..."}`

#### 2. `docker-compose.yml` changes

Add `extra_hosts` to the server service so it can reach the host:

```yaml
server:
  extra_hosts:
    - "host.docker.internal:host-gateway"
  environment:
    - WEBHOOK_URL=http://host.docker.internal:9420
    - WEBHOOK_TOKEN=${WEBHOOK_TOKEN}
```

#### 3. MCP tool: `agents/docker_control.py`

Exposes webhook endpoints as MCP tool calls so Claude can use them naturally:

```python
@tool(tags=["docker"])
def docker_control(action: str, service: str = "") -> str:
    """Manage the Hive Mind Docker stack.

    Actions: status, restart, rebuild, up, down, logs
    Service: server, discord-bot (required for restart/rebuild/logs)
    """
```

This is safer than raw `curl` because the tool validates inputs before calling the webhook.

#### 4. Systemd service (optional)

```ini
[Unit]
Description=Hive Mind Docker Webhook
After=docker.service

[Service]
ExecStart=/home/USER/Storage/Dev/hive_mind/venv/bin/python hive_mind_webhook.py
WorkingDirectory=/home/USER/Storage/Dev/hive_mind
User=daniel
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### Security Properties

- **No Docker socket in container** — Claude cannot construct arbitrary Docker commands
- **Fixed command set** — Only the 6 endpoints above are available
- **Service name allowlist** — Cannot target containers outside this project
- **Loopback binding** — Webhook not reachable from the network, only from the host
- **Auth token** — Prevents other containers from calling the webhook
- **Blast radius** — Worst case: Claude restarts/rebuilds its own stack. No host filesystem access, no other container access.

### Implementation Order

1. Write `hive_mind_webhook.py` (host-side script)
2. Add `extra_hosts` + env vars to `docker-compose.yml`
3. Write `agents/docker_control.py` (MCP tool)
4. Test end-to-end from Discord: `/skill docker_control restart server`
5. (Optional) Create systemd unit for auto-start
