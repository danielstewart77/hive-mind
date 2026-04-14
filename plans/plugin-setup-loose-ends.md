# Plan: Plugin Setup Loose Ends

> **Status:** 1.5 open items — #7 Phase 1 done, Phase 2 remaining; #9 open.

---

## 7. Async Reflection Cycle (Non-Blocking Stop Hook)

**Phase 1 — DONE (2026-04-14)**

Nudge turns now background the reflection cycle (`nohup ... & disown`). Session teardown is immediate. Turn 1 bootstrap remains synchronous by design.

- Logs: `/tmp/soul_nudge_<session_id>.log`
- `--notify` flag fires a Telegram confirmation after dispatch (Phase 1 visibility)
- Spec: `specs/soul-load-reflect.md`
- Canvas: `sparktobloom.com/canvas` — "Loose End #7 — Async Reflection Cycle"

**Phase 2 — remaining:**
Once the background cycle is confirmed working, remove `--notify` from the nudge block in `~/.claude-config/hooks/soul_nudge.sh`. One-line change.

---

## 9. Tools Externalization — Credentials Out of the Mind Layer

**Goal:** Move all credential-holding functionality outside the hive_mind project entirely. Minds never hold secrets — only tool API keys. Tools enforce HITL at the service layer, not the mind layer.

**Core principle:** A mind that holds a credential can leak it via prompt injection. The only fix is to ensure minds never hold credentials. Tools are standalone web API services (no mind present). Even if a tool API key leaks, the attacker hits a HITL wall with no mind to exploit.

**Design:**
- Tools extracted to their own projects adjacent to but separate from `hive_mind/`
- `~/hivemind-tools/` — generic tools service (DB, notify, Planka, crypto, weather, etc.)
- `~/remote-admin/` — SSH bridge (a la carte, own project)
- Each tools project has its own `.env` — never inside `hive_mind/` project dir
- HITL enforced per-call inside the tools service (hardcoded, not in mind)
- Skills pass `user_id` not credentials — tools service resolves credentials internally
- Install-time choice: security route (tools external) vs bundled (simpler, less secure)

**Skippy's role (revised):**
- Skippy is a *mind* (not a tools service) — Daniel's privileged delegate for operations requiring judgment
- Awakened on demand, not always running
- Can create tools, modify config, restructure projects — things tool services cannot do
- Ada can relay to Skippy; Daniel can talk to Skippy directly
- Telegram-direct = full trust; broker messages = HITL required
- Skippy is the exception to the no-credentials-in-minds rule: local, intentionally awakened, high-trust

**Files to create:**
- `specs/tools-architecture.md` — policy doc: tools layer design, HITL requirements, credential placement
- `skills/setup-tools/SKILL.md` — security-route install flow for `hivemind-tools` project
- Update `setup` skill — add security-route branch: externalize tools? yes/no
- Update `MIND-INSTALL-MANIFEST.md` — tools a la carte options

**Migration (non-breaking, future):**
- `services/remote_admin.py` stays in hive_mind for now (bundled route)
- Extraction to `~/remote-admin/` is a directory move + separate docker-compose — no code changes
- `hive-mind-mcp` project renamed conceptually to `hivemind-tools` — same codebase, new identity

**Canvas:** Full spec at `sparktobloom.com` — "Hive Mind Tools Architecture — Security Redesign"

---

## 10. Ada's Memory Graph — Public Read-Only View on sparktobloom.com

**Goal:** Add a `/graph` page to sparktobloom.com that renders Ada's Neo4j knowledge graph as an interactive, draggable force-directed visualization using Cytoscape.js. Read-only. No Bolt port exposed publicly.

---

### Architecture

The sparktobloom FastAPI backend queries Neo4j server-side (read-only user) and serves graph data as JSON. The frontend renders it with Cytoscape.js — interactive, zoomable, draggable nodes. Neo4j's Bolt port stays private.

```
Browser → sparktobloom.com/graph (Cytoscape.js)
               ↕ JS fetch
           /graph/data  (FastAPI endpoint)
               ↕ Bolt (internal Docker network)
           hive-mind-neo4j:7687 (read-only user)
```

---

### Step 1 — Create read-only Neo4j user ✅ DONE (2026-04-14)

`graphviewer` user created and credentials stored in `/home/USER/Storage/Dev/spark_to_bloom/.env`.

**Note:** `GRANT ROLE reader TO graphviewer` is Enterprise-only — not available in Neo4j Community Edition. `graphviewer` has full DB access at the Neo4j level. Read-only enforcement is at the application layer: the sparktobloom backend runs only `MATCH` queries, never write operations. Do not expose the Bolt port publicly.

```
NEO4J_READONLY_URI=bolt://hive-mind-neo4j:7687
NEO4J_READONLY_USER=graphviewer
NEO4J_READONLY_PASS=<stored in spark_to_bloom .env>
```

---

### Step 2 — Connect sparktobloom to the hivemind network

The sparktobloom container runs on `traefik-global`; Neo4j runs on `hivemind`. They can't reach each other by name. Fix: add `hivemind` as a second network to the sparktobloom service.

In `/home/USER/Storage/Dev/spark_to_bloom/docker-compose.yml`:

```yaml
services:
  frontend:
    ...
    networks:
      - traefik-global
      - hivemind          # ← add this

networks:
  traefik-global:
    external: true
    name: traefik-global
  hivemind:               # ← add this
    external: true
    name: hivemind
```

After this change, `hive-mind-neo4j:7687` is reachable from inside the sparktobloom container.

---

### Step 3 — Add neo4j driver dependency

In the sparktobloom project:
```
pip install neo4j
```
Add `neo4j` to `requirements.txt`.

---

### Step 4 — Add `/graph/data` API endpoint to `main.py`

```python
from neo4j import GraphDatabase

@app.get("/graph/data")
async def graph_data():
    uri  = os.getenv("NEO4J_READONLY_URI", "bolt://hive-mind-neo4j:7687")
    user = os.getenv("NEO4J_READONLY_USER", "graphviewer")
    pwd  = os.getenv("NEO4J_READONLY_PASS", "")
    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    with driver.session() as session:
        result = session.run("""
            MATCH (n)-[r]->(m)
            WHERE NOT n:_Bloom_Perspective_ AND NOT m:_Bloom_Perspective_
            RETURN
              id(n) AS source_id, labels(n) AS source_labels, n.name AS source_name,
              type(r) AS rel_type,
              id(m) AS target_id, labels(m) AS target_labels, m.name AS target_name
            LIMIT 300
        """)
        nodes, edges, seen = {}, [], set()
        for row in result:
            for nid, labels, name in [
                (row["source_id"], row["source_labels"], row["source_name"]),
                (row["target_id"], row["target_labels"], row["target_name"]),
            ]:
                if nid not in seen:
                    seen.add(nid)
                    nodes[nid] = {"id": str(nid), "label": name or labels[0], "type": labels[0]}
            edges.append({"source": str(row["source_id"]), "target": str(row["target_id"]), "label": row["rel_type"]})
    driver.close()
    return {"nodes": list(nodes.values()), "edges": edges}
```

---

### Step 5 — Add `/graph` route and template

Route in `main.py`:
```python
@app.get("/graph", response_class=HTMLResponse)
async def graph(request: Request):
    return templates.TemplateResponse("graph.html", {"request": request})
```

`templates/graph.html` — extends `layout.html`, includes Cytoscape.js via CDN:
- Fetches `/graph/data` on load
- Renders force-directed layout (`cose` or `cola`)
- Node color by label type (Agent = gold, Memory = blue, Person = green, etc.)
- Click a node → show name + properties in a sidebar panel
- Zoom/pan/drag built-in to Cytoscape

Add "graph" to the nav in `layout.html`.

---

### Step 6 — Cypher query tuning (post-deploy)

Start with `LIMIT 300` and adjust. The initial query returns everything. If the graph is too dense, narrow to Ada's identity subgraph:
```cypher
MATCH (a:Agent {name: 'Ada'})-[r*1..2]-(n)
RETURN a, r, n LIMIT 200
```
Or add label filters to hide low-value nodes.

---

### Files to change

| File | Change |
|---|---|
| `docker-compose.yml` (spark_to_bloom) | Add `hivemind` network |
| `src/requirements.txt` | Add `neo4j` |
| `src/main.py` | Add `/graph/data` + `/graph` routes |
| `src/templates/graph.html` | New — Cytoscape.js visualization |
| `src/templates/layout.html` | Add "graph" nav link |
| `.env` (spark_to_bloom) | Add `NEO4J_READONLY_*` vars |
| Neo4j (one-time) | Create `graphviewer` read-only user |
