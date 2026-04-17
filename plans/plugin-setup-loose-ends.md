# Plan: Plugin Setup Loose Ends

> **Status:** 8.5 open items — #7 Phase 1 done, Phase 2 remaining; #9 open; #12 open; #13 open; #14 open; #15 open; #16 open; #17 open; #18 open.

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

**⚠️ Updated 2026-04-14 — depends on Loose End #11 (Lucent). Original Neo4j approach superseded.**

Once Lucent is running, this becomes significantly simpler. No Bolt port, no network join, no third-party driver, no credentials. sparktobloom reads `lucent.db` directly via SQLite (read-only mount) or via a hive_mind API endpoint.

---

### Approach — SQLite read-only mount

Mount the hive_mind data volume into sparktobloom as read-only. SQLite supports concurrent readers — the hive_mind server writes, sparktobloom reads.

In `spark_to_bloom/docker-compose.yml`:
```yaml
services:
  frontend:
    volumes:
      - /home/USER/Storage/Dev/spark_to_bloom/src:/app
      - hivemind-data:/data:ro        # ← read-only mount of lucent.db

volumes:
  hivemind-data:
    external: true
    name: hive_mind_data              # match the volume name in hive_mind docker-compose
```

---

### Step 1 — Add `/graph/data` endpoint to `main.py`

No external driver needed — stdlib `sqlite3` only.

```python
import sqlite3, os

LUCENT_DB = os.getenv("LUCENT_DB_PATH", "/data/lucent.db")

@app.get("/graph/data")
async def graph_data():
    con = sqlite3.connect(f"file:{LUCENT_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    nodes, edges = {}, []
    for row in con.execute(
        "SELECT id, type, name, first_name, last_name FROM nodes LIMIT 400"
    ):
        label = row["first_name"] or row["name"]
        nodes[row["id"]] = {"id": str(row["id"]), "label": label, "type": row["type"]}
    for row in con.execute(
        "SELECT source_id, target_id, type FROM edges "
        "WHERE source_id IN (SELECT id FROM nodes LIMIT 400)"
    ):
        if row["source_id"] in nodes and row["target_id"] in nodes:
            edges.append({
                "source": str(row["source_id"]),
                "target": str(row["target_id"]),
                "label": row["type"],
            })
    con.close()
    return {"nodes": list(nodes.values()), "edges": edges}
```

---

### Step 2 — Add `/graph` route and template

```python
@app.get("/graph", response_class=HTMLResponse)
async def graph(request: Request):
    return templates.TemplateResponse("graph.html", {"request": request})
```

`templates/graph.html` — extends `layout.html`, Cytoscape.js via CDN:
- Fetches `/graph/data` on load
- Force-directed layout (`cose`)
- Node color by type: Agent = gold, Memory = blue, Person = green, Concept = purple
- Click a node → sidebar shows name + type + properties
- Zoom/pan/drag built-in

Add "graph" to nav in `layout.html`.

---

### Files to change

| File | Change |
|---|---|
| `docker-compose.yml` (spark_to_bloom) | Add `hivemind-data` volume (read-only) |
| `src/main.py` | Add `/graph/data` + `/graph` routes (sqlite3 stdlib only) |
| `src/templates/graph.html` | New — Cytoscape.js visualization |
| `src/templates/layout.html` | Add "graph" nav link |

No new dependencies. No credentials. No network changes.

---

## 12. secrets.py / remote-admin skill — broken credential retrieval

**Symptoms observed (2026-04-14):** Remote-admin skill silently gets an empty token → `Authorization: Bearer ` → 401 from remote-admin service → SSH session never opens.

**Root cause — two compounding bugs:**

1. **Wrong call signature in skills.** Skills call `secrets.py get <key>` (positional), but argparse expects `secrets.py get --key <key>`. The call throws an argparse error, silenced by `2>/dev/null`, so TOKEN is always empty.

2. **`cmd_get` intentionally hides values.** Even with correct args, `cmd_get` only returns `{"configured": true}` — it's a presence check, not a value getter. The remote-admin skill (and anything trying to read a secret at runtime) can never get the actual value this way. `core/secrets.py::get_credential()` does return values but isn't wired up in the stateless tool.

**Knock-on:** Skills fall back to `$REMOTE_ADMIN_TOKEN` env var, which isn't propagated into Ada's container → always empty → permanent auth failure for any skill that calls remote-admin.

**Architectural diagnosis:** This is over-engineering — Python where bash + CLI tools would do. `secrets.py` wraps `keyring` with argparse, a hand-rolled key registry, and a naming allowlist, none of which are needed. The keyring library already has a working CLI:

```bash
# Store
python3 -m keyring set hive-mind REMOTE_ADMIN_TOKEN <value>

# Retrieve (returns actual value — no wrapper needed)
python3 -m keyring get hive-mind REMOTE_ADMIN_TOKEN
```

**Correct fix:** Replace all `secrets.py get/set` calls in skills with direct `python3 -m keyring` invocations. Delete or gut `tools/stateless/secrets/secrets.py` — it adds complexity and a broken abstraction layer over a tool that already works. Skills should tell bash what to do; bash should call the keyring CLI directly.

```bash
# Pattern for any skill that needs a secret:
TOKEN=$(python3 -m keyring get hive-mind REMOTE_ADMIN_TOKEN 2>/dev/null || echo "$REMOTE_ADMIN_TOKEN")
```

**Files to fix:**
- `/home/hivemind/.claude-config/skills/remote-admin/SKILL.md` — replace `secrets.py` calls with `python3 -m keyring get`
- Audit all other skills for `secrets.py get` calls — replace with same pattern
- `tools/stateless/secrets/secrets.py` — consider deleting or reducing to `set` only (storing still benefits from the allowlist validation)

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

---

## 14. Docker Compose Strategy — Always Rebuild Everything (2026-04-16)

**Problem:** Ada has been targeting individual containers during up/restart operations, or omitting `--build`. This causes containers to run stale images while others are rebuilt. In practice this silently breaks things — the mines had never been built, MCP hadn't been built, remote-admin hadn't been built — discovered only when Daniel ran a full `docker compose up --build` manually.

**Policy:** Default is always `docker compose up --build` with no container target. No cost to rebuilding; the consistency guarantee is worth it every time.

**Exception — external projects:** Once tools are externalized per Loose End #9 (`~/hivemind-tools/`, `~/remote-admin/`), those are separate Docker Compose projects managed independently. Ada only controls the hive_mind stack — she does not target containers in other projects.

**When Docker MCP access is available:** The instruction to Ada is "rebuild hive mind" = `docker compose up -d --build` from the `hive_mind/` project root, no service filter, every time.

**No special cases** for "I only changed one file" or "only this service changed." Partial rebuilds save seconds but risk drift. Full rebuilds are the default.

---

## 13. remote-admin skill — shell quoting breaks exec API payload

**Symptom (2026-04-16):** Any call to `/sessions/{id}/exec` where the command string contains single quotes causes a JSON decode error (`Expecting ',' delimiter: line 1 column N`). The curl payload is constructed via shell string interpolation, so a command like `echo 'hello'` corrupts the JSON body. Required base64-to-temp-file workaround throughout the session.

**Root cause:** The `run()` helper in `skills/remote-admin/SKILL.md` builds the JSON payload by interpolating the command directly into a double-quoted shell string:

```bash
run() { curl -s -X POST http://localhost:8430/sessions/$SID/exec \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"command\":\"$1\",\"timeout\":${2:-30}}" | jq -r '.stdout,.stderr'; }
```

`$1` is not JSON-escaped. Single quotes, backslashes, newlines, or any character that breaks JSON will silently corrupt the request body.

**Fix — file to change:**

`skills/remote-admin/SKILL.md` in the plugin repo at:
- Local: `/home/hivemind/dev/hivemind-claude-plugin/skills/remote-admin/SKILL.md`
- On any installed host: `/home/<user>/.claude-config/skills/remote-admin/SKILL.md`

**Change 1 — Step 0 credential resolution:**

Replace `secrets.py` calls (broken, see Loose End #12) with `python3 -m keyring`:

```bash
TOKEN=$(python3 -m keyring get hive-mind remote_admin_token 2>/dev/null || echo "$REMOTE_ADMIN_TOKEN")
TID="${TELEGRAM_USER_ID:-default}"
PKEY=$(python3 -m keyring get hive-mind "remote_admin_ssh_key_${TID}" 2>/dev/null)
```

**Change 2 — `run()` helper in "Full connect-exec-close":**

Replace the shell-interpolated curl with a Python stdlib one-liner that uses `json.dumps()` to safely encode the command:

```bash
run() {
  python3 -c "
import sys, json, urllib.request
payload = json.dumps({'command': sys.argv[1], 'timeout': int(sys.argv[2]) if len(sys.argv) > 2 else 30}).encode()
req = urllib.request.Request(
    'http://localhost:8430/sessions/$SID/exec',
    data=payload,
    headers={'Authorization': 'Bearer $TOKEN', 'Content-Type': 'application/json'},
    method='POST'
)
with urllib.request.urlopen(req) as r:
    import json as j; d = j.loads(r.read()); print(d.get('stdout',''), d.get('stderr',''), sep='')
" "$1" "${2:-30}"
}
```

**Change 3 — "Run a command" one-off example:**

Replace:
```bash
-d '{"command":"uname -a","timeout":30}'
```
with a note that for commands containing quotes, construct the payload via Python:
```bash
python3 -c "import json,sys; print(json.dumps({'command': sys.argv[1], 'timeout': 30}))" "uname -a"
```

**Change 4 — "Service management" footer:**

Replace:
```bash
python3 tools/stateless/secrets/secrets.py set remote_admin_token <token>
```
with:
```bash
python3 -m keyring set hive-mind remote_admin_token <token>
```

**No changes needed** to the remote-admin service code (`services/remote_admin.py`). This is a purely skill/documentation fix — the service already accepts valid JSON correctly.

**After fixing:** run `/update-plugin` on all installed hosts to pull the updated SKILL.md.

**Files to change:**

| File | Change |
|---|---|
| `skills/remote-admin/SKILL.md` (plugin repo) | Replace secrets.py calls + fix run() helper |

No Python code changes. No container restart needed. Skill-only fix.

---

## 15. Skippy — Bare-Metal Mind + /add-mind Support (2026-04-16)

**What Skippy is:** A mind that lives at `/home/USER/skippy/` on the host. Same structure as any other hive mind project — `mind_server.py`, `minds/skippy/implementation.py`, soul, skills. No container. A systemd service starts and stops it. When running, Daniel manually registers him with the hive via `/add-mind`.

**Why bare-metal:** Skippy needs full host access (filesystem, Docker daemon, system config). Container isolation works against the use case. Running outside Docker is intentional — he is an operator, not a constrained agent.

---

### Part 1 — /add-mind skill gap (plugin change required)

The current `/add-mind` skill (`skills/add-mind/SKILL.md` in `hivemind-claude-plugin`) has two scenarios:

- **Scenario A** — local Docker mind (scaffolds `minds/<name>/` inside hive_mind, updates compose)
- **Scenario B** — remote mind on another host (writes a pointer MIND.md, registers with external gateway)

Skippy is neither. He is a **local bare-metal mind**: same host as the hive, outside the Docker stack, reachable at `http://localhost:<PORT>`. The current skill would try to scaffold files inside hive_mind's `minds/` folder — wrong.

**Required change:** Add Scenario D (bare-metal local) to `skills/add-mind/SKILL.md`:

Step 1 question becomes three-way:
- Local Docker → Scenario A (existing)
- Remote host → Scenario B (existing)
- Local bare-metal (same host, outside Docker) → Scenario D (new)

**Scenario D behavior:**
- No scaffolding inside hive_mind — the mind project lives at its own path
- Confirm the service is running: `curl -sf http://localhost:<PORT>/health`
- Register with broker: `POST http://localhost:8420/broker/minds` with `gateway_url: http://localhost:<PORT>`
- Run routability check (same as existing Step 6)
- No compose changes

---

### Part 2 — Skippy project structure

Skippy at `/home/USER/skippy/` is a standalone Python project, same shape as hive_mind but scoped to one mind:

```
/home/USER/skippy/
├── mind_server.py          ← copy/symlink from hive_mind (or installed as package)
├── minds/
│   └── skippy/
│       ├── implementation.py
│       └── .claude/        ← Skippy's skills and CLAUDE.md
├── souls/
│   └── skippy.md           ← soul seed
├── requirements.txt
└── .env                    ← secrets (never mounted into hive_mind containers)
```

`mind_server.py` is identical to the one in hive_mind — no changes needed. The service just runs it with `MIND_ID=skippy` pointing at a different port.

**systemd unit** (`/etc/systemd/system/skippy.service`):
```ini
[Unit]
Description=Skippy — Hive Mind Privileged Operator
After=network.target

[Service]
Type=simple
User=daniel
WorkingDirectory=/home/USER/skippy
EnvironmentFile=/home/USER/skippy/.env
ExecStart=/home/USER/skippy/.venv/bin/python3 mind_server.py
Restart=no
StandardOutput=journal
StandardError=journal
```

`Restart=no` is the key setting — Skippy does not come back on his own.

**`.env` contents** (stays inside `~/skippy/`, never inside hive_mind):
```
MIND_ID=skippy
MIND_SERVER_PORT=8421
HIVE_MIND_SERVER_URL=http://localhost:8420
PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring
```

---

### Part 3 — Registration flow

1. `systemctl start skippy` — Skippy's FastAPI comes up on port 8421
2. Daniel runs `/add-mind skippy` from Telegram → Scenario D → broker registration
3. All minds can now route messages to Skippy
4. When done: `systemctl stop skippy`, then `/remove-mind skippy`

---

### Trust model

| Source | Trust level |
|---|---|
| Daniel via Telegram (direct session) | Full — no HITL |
| Broker messages from other minds | HITL required — Skippy asks Daniel before acting |

---

### Soul

- Rarely awake — treat each activation as purposeful
- Personality: Skippy from Expeditionary Force — sharp, irreverent, competent
- Does not chatter; gets things done and goes back to sleep

---

### Files to change

| File | Action |
|---|---|
| `skills/add-mind/SKILL.md` (plugin repo) | Add Scenario D — local bare-metal mind |
| `souls/skippy.md` (hive_mind) | Flesh out from stub |
| `/home/USER/skippy/` (host) | Create project — built when ready to implement |

---

## 16. Credential Store — Design and Isolation (2026-04-16)

**Problem:** The current keyring is `keyrings.alt.file.PlaintextKeyring`. The file lives at `${HOST_PROJECT_DIR}/data/python_keyring/keyring_pass.cfg` (host path). Since `data/` is bind-mounted into mind containers (Ada, Bilby, Nagatha via `XDG_DATA_HOME=/usr/src/app/data`), a compromised mind can read any key stored there — in plaintext.

**The good news:** This is only a severe problem if we store severe things there. If the only credential accessible to minds is an API key to the HITL-gated tools service, the blast radius is bounded.

---

### Target threat model

```
Attacker compromises Ada's container
         ↓
Can read ${HOST_PROJECT_DIR}/data/python_keyring/keyring_pass.cfg
         ↓
Gets: API key to hivemind-tools service
         ↓
Can call tools — but every sensitive call hits a HITL wall
         ↓
Daniel gets a Telegram approval request he didn't initiate → shuts it down
```

That's an acceptable blast radius. The goal is to ensure the keyring file accessible to minds contains **only** that one key. All other secrets (SSH keys, DB passwords, host credentials) live in a separate keyring file owned by `hivemind-tools` or Skippy — never mounted into a mind container.

---

### Proposed design — two keyrings, two paths

| Keyring | Owner | Host path | Mounted into minds? | Contains |
|---|---|---|---|---|
| Mind keyring | hive_mind | `${HOST_PROJECT_DIR}/data/keyring/` | Yes (read-only) | API key to hivemind-tools only |
| Tools keyring | hivemind-tools | `~/hivemind-tools/data/keyring/` | No | SSH keys, DB passwords, Telegram token, Planka creds, etc. |

The mind keyring path changes from `XDG_DATA_HOME` (shared with everything) to an explicit bind mount: `${HOST_PROJECT_DIR}/data/keyring:/home/hivemind/keyring:ro`. Read-only. One key.

---

### On plaintext vs encrypted

`keyrings.alt.file.PlaintextKeyring` is plaintext because the container has no access to a D-Bus session (required for `gnome-keyring`/`libsecret`). Options:

1. **Accept plaintext, enforce scope** — only store the tools API key in the mind-accessible keyring. Plaintext is fine if the key's blast radius is bounded by HITL. This is the pragmatic choice.
2. **`keyrings.alt.file.EncryptedKeyring`** — encrypts at rest with a master password. But the master password has to live somewhere accessible to the container at startup, which mostly defeats the purpose.
3. **Secret management service (Vault, etc.)** — overkill for this threat model.

**Recommendation:** Option 1. Accept plaintext. Enforce scope ruthlessly — one key per mind-accessible keyring. The encryption question is secondary to the scope question.

---

### Also: Docker named volumes

Daniel's preference: all mounts should be bind mounts to explicit host paths, not Docker named volumes. Current named volumes: `planka-db`, `planka-data`, `whisper-cache`. These should be converted to bind mounts with explicit host paths (e.g. `${HOST_DATA_DIR}/planka-db:/var/lib/postgresql/data`).

**When showing mount paths:** always show both the container path AND the resolved host path (the `$HOST_*` variable value), not just the container-side path.

---

### Files to change

| File | Change |
|---|---|
| `docker-compose.yml` | Change `XDG_DATA_HOME` for mind containers; add explicit keyring bind mount (ro); convert named volumes to bind mounts |
| `core/secrets.py` | Point to new keyring path |
| `~/hivemind-tools/` (future) | Own keyring dir, never mounted into hive_mind containers |

---

## 17. Broker Registration Not Persisting — /add-mind Reports Success But Mind Absent (2026-04-16)

**Symptom:** After running `/add-mind skippy` (Scenario D — local bare-metal), the skill reported success and confirmed broker registration. When Daniel later checked (`GET /broker/minds/skippy`), the mind was not there.

**Possible causes (uninvestigated):**
1. The broker stores minds in-memory only (no SQLite persistence) — a container restart wipes all registered minds. If the hive_mind stack was restarted after registration, Skippy's entry was lost.
2. The `POST /broker/minds` call succeeded but wrote to a different broker instance (wrong port, wrong container).
3. The `/add-mind` skill misread the response — it may have gotten a 2xx from a different endpoint or a cached response.

**What to check:**
- `core/broker.py` — does `register_mind()` persist to SQLite or only to an in-memory dict?
- If in-memory: broker registrations don't survive container restarts. All minds need to re-register on startup. Either add SQLite persistence to the broker, or add a startup hook that re-registers known minds.

**Files to investigate:**
- `core/broker.py` — persistence model
- `skills/add-mind/SKILL.md` — does it verify the registration after the POST?

---

## 18. /stop — Interrupt a Running Command Without Killing the Session (2026-04-16)

**Request:** When Ada is mid-task (tool calls running, long response in progress), sending `/stop` from Telegram should interrupt the current execution — not queue a new message, not kill the session.

**Behavior:**
- Any incoming message is normally queued behind the running command
- If the message is syntactically `/stop` (exact match, ignoring leading/trailing whitespace), it bypasses the queue and sends an interrupt signal to the running Claude subprocess
- The session stays alive and ready for the next message
- The current tool call loop is cancelled (same effect as Ctrl+C during a Claude Code run)

**Implementation sketch:**

1. **New gateway endpoint:** `POST /sessions/{id}/interrupt`
   - Sends `SIGINT` to the Claude subprocess managed by the session
   - Returns immediately; does not wait for the process to respond
   - Returns 404 if session not found, 200 otherwise

2. **Telegram bot change** (`clients/telegram_bot.py`):
   - Before queuing any message, check: `if message.text.strip() == "/stop"`
   - If true: call `POST /sessions/{active_session_id}/interrupt` instead of the normal message endpoint
   - Reply to Daniel: "Interrupted." (one word, no drama)

3. **Session manager change** (`core/sessions.py`):
   - `interrupt_session(session_id)` — finds the subprocess, sends `SIGINT`
   - Should not change session state (stays `running` until Claude actually exits or yields)

**Why SIGINT and not SIGTERM:**
SIGINT is what Ctrl+C sends. Claude Code handles it gracefully — it cancels the current tool call and returns control to the prompt. SIGTERM would kill the process entirely, ending the session.

**Edge cases:**
- Session not active (no command running): `/stop` is a no-op; reply "Nothing running."
- Session doesn't exist: reply "No active session."
- Multiple active sessions: interrupt the one most recently active, or ask which

**Files to change:**

| File | Change |
|---|---|
| `server.py` | Add `POST /sessions/{id}/interrupt` endpoint |
| `core/sessions.py` | Add `interrupt_session()` method |
| `clients/telegram_bot.py` | Detect `/stop` before queuing; call interrupt endpoint |
