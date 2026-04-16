# Implementation Plan: 1753188531083150484 - Ada Memory Graph (sparktobloom.com /graph)

## Overview

Add a `/graph` page to sparktobloom.com that renders Ada's Lucent knowledge graph as an interactive Cytoscape.js force-directed visualization. The sparktobloom FastAPI backend reads `lucent.db` via a read-only Docker volume mount (stdlib `sqlite3` only), serves graph data as JSON at `/graph/data`, and renders an interactive Cytoscape.js frontend at `/graph`. No credentials, no new dependencies, no network hops.

## Technical Approach

This story modifies the **separate project** at `/home/hivemind/dev/spark_to_bloom/`, not the hive_mind project. All file paths below are relative to that project unless explicitly prefixed.

**Design decisions:**
- SQLite read-only mount via Docker volume -- hive_mind writes to `lucent.db`, sparktobloom reads it. SQLite supports concurrent readers natively.
- Connection opened with `file:{path}?mode=ro` URI to enforce read-only at the driver level.
- Cytoscape.js loaded via CDN -- no npm build step, no new Python dependencies.
- Template extends `layout.html` following the pattern established by `canvas.html` and `home.html`.
- `/graph/data` returns a flat JSON structure `{"nodes": [...], "edges": [...]}` that Cytoscape.js consumes directly.
- Node limit of 400 rows with edge filtering to avoid dangling references.
- Node colors by type: Agent=gold, Memory=blue, Person=green, Concept=purple.

**Cross-project testing strategy:**
Tests for the `/graph/data` endpoint and `/graph` route live in the hive_mind project at `tests/api/test_graph_endpoint.py` and `tests/unit/test_graph_data.py`. These tests import the sparktobloom FastAPI app or test the graph data logic in isolation using in-memory SQLite. This keeps all tests runnable from the hive_mind project where pytest is already configured.

Alternatively, since sparktobloom has no test infrastructure, the most practical approach is to place a standalone test file within the sparktobloom project that can be run with `python3 -m pytest` directly. However, given that the sparktobloom project has no existing tests directory or pytest config, and the modifications are to a different project, tests will be structured as standalone files in `/home/hivemind/dev/spark_to_bloom/tests/`.

## Reference Patterns

| Pattern | Source File | Usage |
|---------|------------|-------|
| Template extending layout.html | `/home/hivemind/dev/spark_to_bloom/src/templates/canvas.html` | Jinja2 `{% extends "layout.html" %}` + `{% block content %}` pattern |
| FastAPI route returning HTML | `/home/hivemind/dev/spark_to_bloom/src/main.py` lines 60-77 (canvas route) | `templates.TemplateResponse("graph.html", {"request": request})` |
| Lucent SQLite schema | `/usr/src/app/tools/stateful/lucent.py` | Table structure for nodes and edges |
| Nav link addition | `/home/hivemind/dev/spark_to_bloom/src/templates/layout.html` line 19-25 | `<li><a href="/graph">graph</a></li>` appended to nav list |
| Docker volume mount | `/usr/src/app/docker-compose.yml` line 10 (`sessions-db`) | External volume pattern |
| In-memory SQLite test pattern | `/usr/src/app/tests/unit/test_lucent_graph.py` | `_make_test_conn()` with `sqlite3.connect(":memory:")` |

## Models & Schemas

No new Pydantic models needed. The `/graph/data` endpoint returns a plain dict:

```python
# Response shape
{
    "nodes": [
        {"id": "1", "label": "Daniel", "type": "Person", "properties": {...}},
        ...
    ],
    "edges": [
        {"source": "1", "target": "2", "label": "MANAGES"},
        ...
    ]
}
```

Node fields come directly from the Lucent `nodes` table. Edge fields from the `edges` table.

## Implementation Steps

### Step 1: Docker-compose volume mount

**Harness-native operation -- no application code needed.**

**Files:**
- Modify: `/home/hivemind/dev/spark_to_bloom/docker-compose.yml` -- add `hivemind-data` external volume (read-only)

**Changes:**
- [ ] Add `hivemind-data:/data:ro` to the `frontend` service `volumes` list
- [ ] Add a top-level `volumes` section declaring `hivemind-data` as external with `name: hive_mind_sessions-db` (this is the Docker volume name created by docker-compose for the hive_mind project's `sessions-db` volume)
- [ ] Verify the volume name matches by checking `docker volume ls | grep sessions` on the host after hive_mind is running

**Result after this step:**
```yaml
services:
  frontend:
    volumes:
      - /home/USER/Storage/Dev/spark_to_bloom/src:/app
      - hivemind-data:/data:ro

volumes:
  hivemind-data:
    external: true
    name: hive_mind_sessions-db
```

---

### Step 2: Graph data extraction function

**Files:**
- Create: `/home/hivemind/dev/spark_to_bloom/tests/__init__.py` -- empty (make tests a package)
- Create: `/home/hivemind/dev/spark_to_bloom/tests/test_graph_data.py` -- unit tests for graph data logic
- Create: `/home/hivemind/dev/spark_to_bloom/src/graph_data.py` -- isolated graph data extraction function

**Test First (unit):** `/home/hivemind/dev/spark_to_bloom/tests/test_graph_data.py`
- [ ] `test_returns_nodes_and_edges` -- asserts function returns dict with "nodes" and "edges" keys from a populated in-memory SQLite DB
- [ ] `test_node_structure_has_required_fields` -- asserts each node has "id", "label", "type" keys
- [ ] `test_edge_structure_has_required_fields` -- asserts each edge has "source", "target", "label" keys
- [ ] `test_node_label_prefers_first_name` -- asserts Person node with first_name uses it as label, falls back to name
- [ ] `test_limits_to_400_nodes` -- inserts 500 nodes, asserts only 400 returned
- [ ] `test_filters_dangling_edges` -- inserts an edge referencing a node outside the 400 limit, asserts edge is excluded
- [ ] `test_empty_database_returns_empty_lists` -- asserts empty nodes/edges when DB has no data
- [ ] `test_read_only_connection_rejects_writes` -- asserts that opening with `?mode=ro` prevents INSERT (raises `sqlite3.OperationalError`)
- [ ] `test_missing_db_file_returns_error` -- asserts graceful error when DB path does not exist

**Then Implement:**
- [ ] Create `graph_data.py` with a `get_graph_data(db_path: str) -> dict` function
- [ ] Use `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)` for read-only access
- [ ] Set `row_factory = sqlite3.Row` for dict-like access
- [ ] Query nodes: `SELECT id, type, name, first_name, last_name, properties FROM nodes LIMIT 400`
- [ ] Build node list with label = `first_name or name`; collect node IDs into a set
- [ ] Query edges: `SELECT source_id, target_id, type FROM edges`
- [ ] Filter edges: only include where both `source_id` and `target_id` are in the node ID set
- [ ] Return `{"nodes": [...], "edges": [...]}`
- [ ] Wrap in try/except; return `{"nodes": [], "edges": [], "error": str(e)}` on failure

**Schema for test DB:** Reuse the schema from `/usr/src/app/tools/stateful/lucent.py` `_init_schema()` -- copy the CREATE TABLE statements for nodes and edges into the test helper.

**Verify:** `cd /home/hivemind/dev/spark_to_bloom && python3 -m pytest tests/test_graph_data.py -v`

---

### Step 3: `/graph/data` and `/graph` FastAPI endpoints

**Files:**
- Modify: `/home/hivemind/dev/spark_to_bloom/src/main.py` -- add `/graph/data` and `/graph` routes
- Create: `/home/hivemind/dev/spark_to_bloom/tests/test_graph_routes.py` -- API tests via TestClient

**Test First (API):** `/home/hivemind/dev/spark_to_bloom/tests/test_graph_routes.py`
- [ ] `test_graph_data_returns_json` -- GET `/graph/data` returns 200 with JSON body containing "nodes" and "edges" keys (use mock/tmp DB)
- [ ] `test_graph_data_uses_lucent_db_path_env` -- asserts endpoint reads from `LUCENT_DB_PATH` env var
- [ ] `test_graph_page_returns_html` -- GET `/graph` returns 200 with HTML content type
- [ ] `test_graph_page_contains_cytoscape_reference` -- GET `/graph` response body contains "cytoscape" (CDN script tag)
- [ ] `test_graph_data_handles_missing_db` -- GET `/graph/data` when DB does not exist returns JSON with empty nodes/edges (not 500)

**Then Implement:**
- [ ] Add `import os, sqlite3` at top of `main.py` (sqlite3 and os are stdlib -- no new deps)
- [ ] Add `from graph_data import get_graph_data` import
- [ ] Add `LUCENT_DB = os.getenv("LUCENT_DB_PATH", "/data/lucent.db")` constant
- [ ] Add `/graph/data` endpoint (GET, returns JSON):
  ```python
  @app.get("/graph/data")
  async def graph_data():
      return get_graph_data(LUCENT_DB)
  ```
- [ ] Add `/graph` endpoint (GET, returns HTML):
  ```python
  @app.get("/graph", response_class=HTMLResponse)
  async def graph(request: Request):
      return templates.TemplateResponse("graph.html", {"request": request})
  ```
- [ ] Follow the pattern from the `canvas` route (line 60-77 of `main.py`)

**Verify:** `cd /home/hivemind/dev/spark_to_bloom && python3 -m pytest tests/test_graph_routes.py -v`

---

### Step 4: graph.html template (Cytoscape.js visualization)

**Files:**
- Create: `/home/hivemind/dev/spark_to_bloom/src/templates/graph.html` -- Cytoscape.js visualization page

**Harness-native operation for template creation -- no unit tests for HTML/JS templates.**

Template requirements (validated by API tests in Step 3):
- [ ] Extends `layout.html` using `{% extends "layout.html" %}` / `{% block content %}`
- [ ] Include Cytoscape.js CDN: `<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.4/cytoscape.min.js"></script>`
- [ ] Full-page layout: container div (`#cy`) fills available space (min-height 80vh), with a collapsible sidebar (`#sidebar`) for node details
- [ ] On page load, `fetch('/graph/data')` and populate Cytoscape instance
- [ ] Convert API response to Cytoscape elements format:
  ```javascript
  nodes.map(n => ({ data: { id: n.id, label: n.label, type: n.type, properties: n.properties } }))
  edges.map(e => ({ data: { source: e.source, target: e.target, label: e.label } }))
  ```
- [ ] Use `cose` layout (force-directed, built into Cytoscape.js)
- [ ] Node color by type via Cytoscape style selectors:
  - `[type = "Agent"]` -> `background-color: #c9a84c` (gold, matching sparktobloom accent)
  - `[type = "Memory"]` -> `background-color: #38bdf8` (blue, matching sparktobloom accent)
  - `[type = "Person"]` -> `background-color: #4ade80` (green)
  - `[type = "Concept"]` -> `background-color: #a78bfa` (purple)
  - Default -> `background-color: #94a3b8` (slate, for any other type like Project, System, Preference)
- [ ] Node label: `data(label)` displayed inside/below node
- [ ] Edge label: `data(label)` displayed on edge
- [ ] Click handler: on node tap, populate sidebar with node name, type, and properties (JSON formatted)
- [ ] Sidebar styling: dark background matching site palette (`rgba(10, 14, 25, 0.90)`), positioned absolute right
- [ ] Graph container styling: dark background matching site palette
- [ ] Zoom, pan, drag are Cytoscape.js defaults (no extra code needed)
- [ ] Loading state: show "Loading graph..." text while fetch is in progress
- [ ] Error state: show message if fetch fails

**Design notes:**
- Follow `canvas.html` pattern: `{% extends "layout.html" %}`, page-specific `<style>` block, JS at bottom
- Colors should harmonize with the site's Summer Techy 2026 palette (deep navy `#162030`, ice blue `#dce8f0`, sky blue accent `#38bdf8`)
- Graph container background should be dark (`#0a0e19`) to make colored nodes visible

---

### Step 5: Update layout.html navigation

**Harness-native operation -- no application code or tests needed.**

**Files:**
- Modify: `/home/hivemind/dev/spark_to_bloom/src/templates/layout.html` -- add "graph" nav link

**Changes:**
- [ ] Add `<li><a href="/graph">graph</a></li>` to the `#nav-links` `<ul>`, after the "linkedin" entry (line 25)

**Result after this step:**
```html
<li><a href="/linkedin">linkedin</a></li>
<li><a href="/graph">graph</a></li>
```

---

## Integration Checklist

- [ ] Docker volume mount: `hivemind-data:/data:ro` in spark_to_bloom `docker-compose.yml`
- [ ] External volume declared with correct name matching hive_mind's `sessions-db` volume
- [ ] `/graph/data` route added to `main.py`
- [ ] `/graph` route added to `main.py`
- [ ] `graph_data.py` created with `get_graph_data()` function
- [ ] `graph.html` template created extending `layout.html`
- [ ] "graph" link added to `layout.html` navigation
- [ ] No new entries in `requirements.txt` (stdlib sqlite3 + CDN Cytoscape.js only)
- [ ] No credentials, secrets, or env vars beyond `LUCENT_DB_PATH` (with sensible default)
- [ ] All operations are read-only (no writes to lucent.db)

## Build Verification

- [ ] `cd /home/hivemind/dev/spark_to_bloom && python3 -m pytest tests/ -v` passes
- [ ] `docker compose up -d --build` (spark_to_bloom) starts without errors
- [ ] `curl localhost:5000/graph/data` returns valid JSON with nodes and edges
- [ ] `curl localhost:5000/graph` returns HTML with Cytoscape.js script tag
- [ ] Browser: `sparktobloom.com/graph` renders interactive force-directed graph
- [ ] Browser: clicking a node shows sidebar with node details
- [ ] Browser: navigation bar includes "graph" link
- [ ] All ACs addressed

## AC-to-Test Mapping

| Acceptance Criterion | Test |
|---------------------|------|
| Docker-compose hivemind-data volume | Harness-native (Step 1) |
| `/graph/data` returns JSON graph structure | `test_graph_data_returns_json`, `test_returns_nodes_and_edges` |
| Uses stdlib sqlite3 only | `test_read_only_connection_rejects_writes` (no imports beyond stdlib) |
| Queries up to 400 nodes | `test_limits_to_400_nodes` |
| `/graph` returns graph.html | `test_graph_page_returns_html` |
| Cytoscape.js force-directed visualization | `test_graph_page_contains_cytoscape_reference` |
| Extends layout.html | Template structure (harness-verified) |
| Node colors by type | Template CSS (harness-verified) |
| Click node shows sidebar | Template JS (harness-verified, manual browser test) |
| Zoom/pan/drag | Cytoscape.js defaults (no code needed) |
| layout.html graph nav link | Harness-native (Step 5) |
| No new dependencies | No changes to requirements.txt |
| Public and read-only | `test_read_only_connection_rejects_writes` |
