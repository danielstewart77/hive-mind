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

---

## 11. Lucent — Replace Neo4j with Owned Graph + Vector Store

**Why:** Neo4j is a third-party container with no keyring support. Its password lives in `.env` permanently. Replacing it with a SQLite-backed store we own eliminates the `.env` dependency, removes one Docker service, and gives us a simpler deployment with better security posture.

**Why it's viable:** Every Cypher query in the codebase maps directly to standard SQL. Neo4j's vector index (`CALL db.index.vector.queryNodes`) is replaced with brute-force numpy cosine similarity — at our scale (few thousand memories, 4096-dim) this is microseconds. No APOC procedures are used anywhere in production code.

**Name:** Lucent — lightweight, clear, ours.

---

### Schema — `data/lucent.db` (single SQLite file)

```sql
-- Knowledge graph nodes
CREATE TABLE nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT    NOT NULL,
    type        TEXT    NOT NULL,   -- Person, Project, System, Concept, Preference, Agent
    name        TEXT    NOT NULL,
    first_name  TEXT,               -- Person nodes — indexed for search_person()
    last_name   TEXT,               -- Person nodes — indexed for search_person()
    properties  TEXT    DEFAULT '{}', -- JSON blob for everything else
    data_class  TEXT,
    tier        TEXT,
    source      TEXT,
    as_of       TEXT,
    created_at  REAL,
    updated_at  REAL,
    UNIQUE(agent_id, name)
);
CREATE INDEX idx_nodes_agent_type  ON nodes(agent_id, type);
CREATE INDEX idx_nodes_first_name  ON nodes(agent_id, first_name);
CREATE INDEX idx_nodes_last_name   ON nodes(agent_id, last_name);

-- Knowledge graph edges
CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT    NOT NULL,
    source_id   INTEGER NOT NULL REFERENCES nodes(id),
    target_id   INTEGER NOT NULL REFERENCES nodes(id),
    type        TEXT    NOT NULL,   -- KNOWS_ABOUT, WORKS_ON, PREFERS, etc.
    as_of       TEXT,
    source      TEXT,
    data_class  TEXT,
    tier        TEXT,
    created_at  REAL,
    UNIQUE(source_id, target_id, type)
);

-- Semantic memory (vector store)
CREATE TABLE memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    embedding    BLOB,              -- numpy float32 array, tobytes()
    tags         TEXT    DEFAULT '',
    source       TEXT,
    data_class   TEXT,
    tier         TEXT,
    as_of        TEXT,
    expires_at   TEXT,
    superseded   INTEGER DEFAULT 0,
    recurring    INTEGER,
    codebase_ref TEXT,
    created_at   INTEGER
);
CREATE INDEX idx_memories_agent ON memories(agent_id);
CREATE INDEX idx_memories_expires ON memories(expires_at);
```

---

### Python API — drop-in replacement

The public API surface of `tools/stateful/knowledge_graph.py` and `tools/stateful/memory.py` stays **identical**. Same function signatures, same JSON return shapes, same MCP tool names. Zero changes required in skills, agents, or calling code.

Internal implementation changes:

| Neo4j Cypher | Lucent SQLite / Python |
|---|---|
| `MERGE (n:Label {name, agent_id}) SET n += $props` | `INSERT INTO nodes ... ON CONFLICT(agent_id, name) DO UPDATE SET ...` |
| `MERGE (n)-[r:TYPE]->(m)` | `INSERT OR IGNORE INTO edges ...` |
| `MATCH (n) WHERE n.name = $x OR n.first_name = $x ... OPTIONAL MATCH (n)-[r*1..N]-(m)` | Python BFS: `SELECT * FROM edges WHERE source_id=? OR target_id=?` up to depth N |
| `MATCH (n:Person) WHERE toLower(n.first_name) CONTAINS toLower($x)` | `WHERE lower(first_name) LIKE '%' \|\| lower(?) \|\| '%'` |
| `CALL db.index.vector.queryNodes($index, $k, $embedding)` | numpy: `scores = embeddings_matrix @ query_vec; top_k = argsort(scores)[-k:]` |
| `elementId(n)` as string ID | `CAST(id AS TEXT)` — same behaviour to callers |

---

### Vector similarity (memory_retrieve)

On startup, load all embeddings for `agent_id` into a numpy matrix (lazy, cached). On query:

```python
import numpy as np

def _cosine_retrieve(query_embedding, agent_id, k, tag_filter=None):
    rows = db.execute("SELECT id, embedding, content, ... FROM memories WHERE agent_id=?", [agent_id])
    ids, vecs, meta = [], [], []
    for row in rows:
        if tag_filter and tag_filter not in (row["tags"] or ""):
            continue
        vec = np.frombuffer(row["embedding"], dtype=np.float32)
        ids.append(row["id"])
        vecs.append(vec)
        meta.append(row)
    if not vecs:
        return []
    matrix = np.stack(vecs)
    q = np.array(query_embedding, dtype=np.float32)
    scores = matrix @ q / (np.linalg.norm(matrix, axis=1) * np.linalg.norm(q) + 1e-9)
    top_k = np.argsort(scores)[::-1][:k]
    return [(meta[i], float(scores[i])) for i in top_k]
```

At 5,000 memories × 4096 dims: ~80MB in RAM, sub-millisecond per query. Trivially acceptable.

---

### Implementation steps

1. **New file: `tools/stateful/lucent.py`** — SQLite connection, schema init, all helper functions
2. **New file: `tools/stateful/lucent_graph.py`** — drop-in replacement for `knowledge_graph.py` (same `KG_TOOLS` list, same function signatures)
3. **New file: `tools/stateful/lucent_memory.py`** — drop-in replacement for `memory.py` (same `MEMORY_TOOLS` list)
4. **`mcp_server.py`** — swap import: `from tools.stateful.lucent_graph import KG_TOOLS` etc. (one-line change per tool file)
5. **`tools/stateless/lucent_migrate.py`** — one-time migration script: reads Neo4j, writes to `lucent.db`. Run manually before cutover.
6. **`docker-compose.yml`** — remove `neo4j` service and `neo4j-data` volume after migration confirmed
7. **`.env`** — remove `NEO4J_AUTH` and `NEO4J_URI` after cutover

---

### Migration plan (non-destructive)

1. Build and test Lucent against an empty DB — all tools work
2. Run `lucent_migrate.py` — exports Neo4j → `lucent.db` (Neo4j stays up)
3. Switch `mcp_server.py` imports to Lucent — restart MCP container
4. Run smoke tests: `graph_query`, `memory_retrieve`, `search_person`
5. If good: remove Neo4j service, remove `.env` entries
6. If bad: revert `mcp_server.py` imports, Neo4j is still running

No data loss risk — Neo4j untouched until step 6.

---

### Downstream effect on Loose End #10

If Lucent ships before the sparktobloom graph view is built, the `/graph/data` endpoint in `main.py` queries `lucent.db` directly via SQLite instead of Neo4j. Simpler, faster, no `graphviewer` user needed, no network hop. The `graphviewer` setup (Step 1 of #10) becomes unnecessary.

---

### Files

| File | Action |
|---|---|
| `tools/stateful/lucent.py` | New — SQLite core (schema, connection, helpers) |
| `tools/stateful/lucent_graph.py` | New — KG tools (drop-in for `knowledge_graph.py`) |
| `tools/stateful/lucent_memory.py` | New — memory tools (drop-in for `memory.py`) |
| `tools/stateless/lucent_migrate.py` | New — one-time Neo4j → SQLite migration script |
| `mcp_server.py` | Swap imports (2 lines) |
| `docker-compose.yml` | Remove `neo4j` service (post-migration) |
| `.env` | Remove `NEO4J_AUTH`, `NEO4J_URI` (post-migration) |
