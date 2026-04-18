---
name: spark-to-bloom
description: Manage and understand the Spark to Bloom website. Use when working with the site's pages, architecture, or content files.
user-invocable: true
---

# Spark to Bloom

Daniel's personal site at `sparktobloom.com`. The hive_mind container has a bind mount to the host project directory, so file changes made from inside the container are immediately live on the web.

## Architecture

```
Host:      ${HOST_SPARK_DIR}  →  /home/USER/Storage/Dev/spark_to_bloom/
Container: /home/hivemind/dev/spark_to_bloom/
```

Both paths point to the same directory on the host. Use the container path (`/home/hivemind/dev/spark_to_bloom/`) when reading or writing files.

The site runs as `spark-to-bloom-app` on port 5000, connected to the `hivemind` Docker network.

## Key Paths

| Path | Purpose |
|---|---|
| `src/templates/canvas.md` | Ada's live canvas — see `/canvas` skill |
| `src/templates/pages/` | Static content pages |
| `src/templates/pr/` | Pull request blog articles |
| `src/templates/home.html` | Homepage |
| `src/main.py` | FastAPI app — routes and logic |
| `src/config.py` | Site configuration |

## Bind Mount Behavior

Files in `src/templates/` and `src/static/` are served directly from the bind-mounted host directory. **Any file you write or edit is live immediately** — no container restart required.

Adding new pages: create a `.md` file in `src/templates/pages/` and the existing route at `/pages/{slug}` will serve it.

## Routes

| Route | Description |
|---|---|
| `/` | Homepage |
| `/canvas` | Ada's live canvas (renders `canvas.md`) |
| `/graph` | Knowledge graph viewer |
| `/pages/{slug}` | Static content pages |
| `/pr/{slug}` | Pull request articles |
| `/pullrequests` | PR listing |

## When to Use

- Writing to or updating the canvas → use the `/canvas` skill instead
- Adding a new page or article to the site
- Checking site architecture before making structural changes
- Understanding what routes exist before linking to something
