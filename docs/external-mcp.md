# External MCP Server (hive_mind_mcp)

Hive Mind uses two MCP servers:

| Server | Transport | Purpose |
|---|---|---|
| `hive-mind-tools` | stdio (spawned by Claude CLI) | Internal tools — memory, knowledge graph, Planka, notifications, reminders, self-improvement |
| `hive-mind-mcp` | SSE over HTTP (separate Docker service) | External tools — Gmail, Google Calendar, Docker Compose ops |

## Why Two Servers?

The internal server (`mcp_server.py`) runs as a subprocess of Claude CLI inside the main container. It has direct access to the Python runtime, the keyring, and the filesystem.

The external server (`hive_mind_mcp`) is a standalone FastAPI service. It exists because some tools need capabilities that are awkward to co-locate with the main container:

- **OAuth credentials** (Gmail, Calendar) — use file-based token storage at `hive_mind_mcp/credentials/`
- **Docker socket access** — mounting `/var/run/docker.sock` into the gateway container would be a significant security expansion; the MCP container is purpose-built for it
- **Separation of concerns** — tools that manage infrastructure (compose ops) are isolated from the AI runtime they're managing

## Architecture

```
Claude CLI subprocess (inside hive_mind container)
  │
  │  SSE + bearer token
  ▼
hive-mind-mcp container (port 9421, hivemind network only)
  │
  ├── tools/gmail.py        (Google OAuth token)
  ├── tools/calendar.py     (Google OAuth token)
  ├── tools/linkedin.py     (OAuth token)
  ├── tools/docker_ops.py   (Docker socket)
  └── tools/approval.py     (HITL gate → calls gateway /hitl/request)
```

## Authentication

The SSE endpoint (`/sse`) is protected by a bearer token:

```json
// .mcp.container.json
{
  "mcpServers": {
    "hive-mind-mcp": {
      "type": "sse",
      "url": "http://hive-mind-mcp:9421/sse",
      "headers": {
        "Authorization": "Bearer ${MCP_AUTH_TOKEN}"
      }
    }
  }
}
```

`MCP_AUTH_TOKEN` is stored in the keyring and bridged into the environment at gateway startup. The connection is confined to the `hivemind` Docker network — the port is not exposed externally.

## Adding a Tool to hive_mind_mcp

1. Create `hive_mind_mcp/tools/my_tool.py` with async functions
2. Import and register in `hive_mind_mcp/server.py`:
   ```python
   from tools.my_tool import my_function
   mcp.tool()(my_function)
   ```
3. For write operations, gate with HITL:
   ```python
   from tools.approval import require_approval

   async def my_write_tool(param: str) -> str:
       denied = await require_approval("my_write_tool", f"Doing: {param}")
       if denied:
           return denied
       # ... proceed
   ```
4. Restart the `hive_mind_mcp` container to pick up changes

Note: unlike internal `agents/` tools, external MCP tools are **not** auto-discovered. They must be explicitly imported and registered in `server.py`.

## Credential Storage

Credentials are stored in `hive_mind_mcp/credentials/` (mounted at `/app/credentials` inside the container):

| File | Contents |
|---|---|
| `token.json` | Google OAuth (Gmail + Calendar) |
| `linkedin_token.json` | LinkedIn OAuth access token |
| `credentials.json` | Google OAuth app credentials |

The credentials volume is mounted read-only inside `hive_mind_mcp`. The `hive_mind` server writes new tokens to this path (it has a writable mount) and the MCP container reads them.

## HITL in External Tools

The `tools/approval.py` module in `hive_mind_mcp` implements non-blocking HITL polling. It sends a POST to the gateway's `/hitl/request`, then polls `/hitl/status/{token}` every 5 seconds. This keeps the MCP SSE connection alive during long waits (a single long-blocking request would time out the SSE client).

See [HITL documentation](hitl.md) for the full approval flow.
