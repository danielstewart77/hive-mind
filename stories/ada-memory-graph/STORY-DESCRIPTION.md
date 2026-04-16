# Ada memory graph — sparktobloom.com /graph page

**Card ID:** 1753188531083150484

## Description

Add a `/graph` page to sparktobloom.com that renders Ada's knowledge graph as an interactive, draggable force-directed visualization using Cytoscape.js. Read-only. No Bolt port exposed publicly.

## Goal

Provide a public, interactive visualization of Ada's memory graph at sparktobloom.com/graph using Cytoscape.js. The visualization is force-directed, draggable, zoomable, and displays node types with distinct colors. No credentials or private network exposure.

## Architecture

The sparktobloom FastAPI backend reads Ada's graph data via SQLite (not Neo4j Bolt). This depends on Loose End #11 (Lucent) being complete first.

```
Browser → sparktobloom.com/graph (Cytoscape.js frontend)
               ↕ JS fetch
           /graph/data  (FastAPI endpoint)
               ↕ SQLite read
           lucent.db (read-only Docker volume mount)
```

The approach:
- Mount the hive_mind data volume into sparktobloom container as read-only
- SQLite supports concurrent readers — hive_mind server writes, sparktobloom reads
- No third-party driver needed — stdlib `sqlite3` only
- No credentials, no network complexity

## Acceptance Criteria

- [ ] Docker-compose setup: Add `hivemind-data` volume to `spark_to_bloom/docker-compose.yml` as read-only mount
- [ ] `/graph/data` endpoint: FastAPI route returns JSON graph structure with nodes and edges from lucent.db
- [ ] `/graph/data` endpoint: Uses stdlib `sqlite3` only (no external drivers)
- [ ] `/graph/data` endpoint: Queries up to 400 nodes and their edges efficiently
- [ ] `/graph` route: FastAPI endpoint returns graph.html template
- [ ] graph.html template: Renders Cytoscape.js force-directed visualization (`cose` layout)
- [ ] graph.html template: Extends layout.html properly (includes base styling, navigation)
- [ ] Node colors: Agent = gold, Memory = blue, Person = green, Concept = purple
- [ ] Interactive features: Click node → sidebar shows name + type + properties
- [ ] Interactive features: Zoom, pan, drag built-in (Cytoscape.js default)
- [ ] layout.html: Add "graph" link to navigation menu
- [ ] No new dependencies required (Cytoscape.js via CDN)
- [ ] Page is public and read-only (no write operations)

## Tasks

1. Update `docker-compose.yml` (spark_to_bloom service)
   - Add `hivemind-data` volume with read-only access
   - Ensure volume name matches hive_mind docker-compose

2. Implement `/graph/data` endpoint in `src/main.py`
   - Use stdlib sqlite3 only
   - Read from LUCENT_DB_PATH env var (default: `/data/lucent.db`)
   - Query nodes table: `SELECT id, type, name, first_name, last_name FROM nodes LIMIT 400`
   - Query edges table filtered to returned nodes
   - Return JSON: `{"nodes": [...], "edges": [...]}`
   - Node structure: `{"id": str, "label": str, "type": str}`
   - Edge structure: `{"source": str, "target": str, "label": str}`

3. Implement `/graph` route in `src/main.py`
   - Serve `templates/graph.html` with Cytoscape.js

4. Create `src/templates/graph.html`
   - Extends `layout.html`
   - Cytoscape.js CDN link
   - Fetch `/graph/data` on page load
   - Initialize force-directed layout (cose)
   - Apply node colors by type
   - Implement click handler: show properties in sidebar
   - Include zoom/pan/drag controls

5. Update `src/templates/layout.html`
   - Add "graph" to navigation menu

## Depends On

- Loose End #11 (Lucent) must be complete first
- lucent.db must be populated with node and edge data
- hive_mind docker-compose must expose `hive_mind_data` volume

## Files to Change

| File | Change |
|---|---|
| `docker-compose.yml` (spark_to_bloom) | Add `hivemind-data` volume (read-only) |
| `src/main.py` | Add `/graph/data` endpoint + `/graph` route |
| `src/templates/graph.html` | New — Cytoscape.js visualization |
| `src/templates/layout.html` | Add "graph" nav link |

## Notes

- No new Python dependencies (Cytoscape.js loads via CDN)
- No credentials needed (read-only SQLite mount)
- SQLite read-only mode specified in connection URI: `file:{path}?mode=ro`
- Node label priority: first_name > name (for Person nodes, show first name)
- Edge filtering: only include edges where both source and target exist in returned nodes (avoid dangling references)
