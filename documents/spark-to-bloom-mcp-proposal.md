# Spark to Bloom — MCP Content Server: Implementation Proposal (v2)

## Architecture

A dedicated MCP server lives **inside** the `spark_to_bloom` project. Ada accesses
all content and git operations through MCP tool calls — no direct filesystem access,
no cross-project bind mounts.

```
Ada (hive_mind container)
  │
  │  MCP protocol (SSE, bearer token)
  ▼
spark-to-bloom-mcp  ←── new service in spark_to_bloom/docker-compose.yml
  │
  │  file I/O (own project files only)
  │  git subprocess
  ▼
spark_to_bloom source tree (src/templates/, etc.)
```

This mirrors the existing pattern: `hive_mind` → `hive_mind_mcp` over MCP.

---

## Changes Required

### 1. spark_to_bloom — new MCP server

New files in `/home/USER/Storage/Dev/spark_to_bloom/`:

```
mcp_server.py           ← FastMCP server entry point
tools/
  __init__.py
  content.py            ← article + page CRUD
  homepage.py           ← homepage grid read/update
  git_ops.py            ← git status, diff, commit, push
requirements-mcp.txt    ← mcp[cli], uvicorn, starlette, python-dotenv
Dockerfile.mcp          ← separate Dockerfile for the MCP service
```

The MCP server service runs alongside the existing FastAPI app in the same
`docker-compose.yml` and has access to the project files naturally (same directory).

### 2. spark_to_bloom — docker-compose.yml

Add a new service:

```yaml
services:
  app:
    # existing FastAPI service — unchanged

  mcp:
    build:
      context: .
      dockerfile: Dockerfile.mcp
    container_name: spark-to-bloom-mcp
    env_file: .env.mcp            # MCP_AUTH_TOKEN
    volumes:
      - .:/app                    # project source (read/write for content edits)
    restart: unless-stopped
    networks:
      - traefik-global
      - hivemind                  # so hive_mind container can reach it
```

### 3. hive_mind — .mcp.container.json

Register the new server alongside the existing `hive-mind-mcp` entry:

```json
{
  "mcpServers": {
    "hive-mind-mcp": { ... existing ... },
    "spark-to-bloom-mcp": {
      "type": "sse",
      "url": "http://spark-to-bloom-mcp:9422/sse",
      "headers": {
        "Authorization": "Bearer ${SPARK_TO_BLOOM_MCP_TOKEN}"
      }
    }
  }
}
```

`SPARK_TO_BLOOM_MCP_TOKEN` stored in keyring, bridged to env in `server.py` startup
(same pattern as `MCP_AUTH_TOKEN`).

---

## MCP Tools

### Content — articles

| Tool | HITL | Description |
|---|---|---|
| `stb_list_articles()` | No | List all `.md` files in `src/templates/pr/` with titles |
| `stb_get_article(slug)` | No | Return raw markdown for an article |
| `stb_create_article(slug, title, content)` | Yes | Write new `.md` + add link to `pullrequests.html` |
| `stb_update_article(slug, content)` | Yes | Overwrite `.md` file |
| `stb_update_article_title(slug, title)` | Yes | Update link text in `pullrequests.html` |
| `stb_delete_article(slug)` | Yes | Delete `.md` + remove link from `pullrequests.html` |

### Content — pages

| Tool | HITL | Description |
|---|---|---|
| `stb_list_pages()` | No | List all `.md` files in `src/templates/pages/` |
| `stb_get_page(slug)` | No | Return raw markdown for a page |
| `stb_create_page(slug, content)` | Yes | Write new `.md` file |
| `stb_update_page(slug, content)` | Yes | Overwrite `.md` file |
| `stb_delete_page(slug)` | Yes | Delete `.md` file |

### Homepage grid

| Tool | HITL | Description |
|---|---|---|
| `stb_get_home_grid()` | No | Return HTML between `<!-- grid-start -->` / `<!-- grid-end -->` sentinels |
| `stb_update_home_grid(html)` | Yes | Replace grid region with provided HTML |

One-time manual step: add sentinel comments to `home.html` to define the editable region.

### Git / deploy

| Tool | HITL | Description |
|---|---|---|
| `stb_git_status()` | No | `git status --short` |
| `stb_git_diff(path)` | No | `git diff` (whole project or specific file) |
| `stb_publish(message)` | Yes | `git add src/templates/ && git commit -m "..." && git push` — Woodpecker CI picks up the push and redeploys |

---

## HITL

All write operations require HITL approval via the existing `hive_mind` gateway,
same as `hive_mind_mcp`. The `spark-to-bloom-mcp` container needs
`HIVE_MIND_SERVER_URL=http://hive-mind-server:8420` in its env and a copy of
the `tools/approval.py` pattern (or a shared package — see Open Questions).

---

## Open Questions

1. **Shared approval utility** — `tools/approval.py` currently lives only in
   `hive_mind_mcp`. Options:
   - Copy it into `spark_to_bloom` (simple, slight duplication)
   - Extract it into a shared PyPI package (clean, more overhead)
   - Have the `spark-to-bloom-mcp` call `hive_mind_mcp`'s approval endpoint
     directly (avoids duplication, tighter coupling)

   Recommendation: copy for now, extract later if a third project needs it.

2. **Homepage grid sentinels** — the sentinel comments need to be added to
   `home.html` manually before `stb_get_home_grid` / `stb_update_home_grid` work.
   Should the tool auto-insert them on first use, or should Daniel add them manually?

3. **Git identity** — `spark_to_bloom` presumably already has a repo-level
   `git config user.name / user.email`. If not, needs to be set in `Dockerfile.mcp`
   or passed as env vars.

4. **Port** — proposal uses `9422`. Needs to not conflict with anything else on
   the `hivemind` network.

5. **Woodpecker visibility** — do we want a `stb_build_status()` tool that checks
   the last Woodpecker CI run? Would need a Woodpecker API token.
