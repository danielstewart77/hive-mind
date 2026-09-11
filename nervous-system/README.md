# Nervous system

The hive's shared data and signal plane: **lucent** (vector store +
knowledge graph) and **comms** (gateway — session manager, broker, HITL,
remote control). Two containers, built and run from the repo-root
`docker-compose.yml` as services `lucent` and `comms`.

Minds hold none of this code. They reach both services over HTTP with a
bearer token — `hive-lucent:8424` and `hive-comms:8424` on the `hivemind`
Docker network from containers, `127.0.0.1:8425` and `127.0.0.1:8426`
from the host and LAN. One lucent database, many writers, provenance
recorded per write but no `mind_id` filter on reads (every mind sees
everything).

## What's in here

- **`lucent_api/`** — FastAPI app with two routers: `/memory/*` (vector
  store) and `/graph/*` (knowledge graph). Bearer-gated on every route
  except `/health`. Also carries the support modules: data class
  registry (`memory_schema.py`), four-class pruner (`prune_memory.py`),
  shared Telegram notifier (`notify_utils.py`), secrets adapter
  (`secrets.py`), and its own test suite under `lucent_api/tests/`.
- **`comms/`** — the gateway. Session manager (`sessions.py`), broker
  (`broker.py`), bootstrap prompt composition (`bootstrap_loader.py`),
  per-turn contextual retrieval (`contextual_retrieval.py`), SMS inbound
  (`sms_inbound.py`), and the FastAPI server (`server.py`). Has its own
  `Dockerfile` and `requirements.txt`; the container runs
  `uvicorn comms.server:app`.
- **`server.py`** — lucent container entry. Starts uvicorn + an
  APScheduler in-process cron (`prune-memory` daily at 4am).
- **`Dockerfile`, `requirements.txt`** — the lucent image.
- **`tests/`** — the comms test suite.
- **`scripts/migrations/`** — one-shot DB migrations (archived after
  run).
- **`data/`** — runtime databases (gitignored): `lucent.db`,
  `broker.db`, `sessions.db`, mounted at `/data` in both containers.

## Quick start

```bash
# From the repo root.

# 1. Set bearer tokens
cp nervous-system/.env.example nervous-system/.env
# then fill in LUCENT_BEARER_TOKEN, COMMS_BEARER_TOKEN, ...

# 2. Make sure the external `hivemind` docker network exists
docker network create hivemind 2>/dev/null || true

# 3. Bring both up
docker compose up -d --build lucent comms

# 4. Health checks
curl http://127.0.0.1:8425/health
curl http://127.0.0.1:8426/health
```

Both containers bind-mount their source (`lucent_api/`, `comms/`), so
code edits deploy with a container restart — no rebuild. `server.py`
changes need a rebuild of the lucent image.

## Test

```bash
cd nervous-system
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r comms/requirements.txt \
    pytest pytest-asyncio httpx
.venv/bin/python -m pytest
```

`pytest.ini` here collects both suites (`tests/` for comms,
`lucent_api/tests/` for lucent), separate from the repo-root suite.

## Endpoints (lucent)

All bearer-gated except `/health`.

| Path | Purpose |
|---|---|
| `GET /health` | open — `{"status":"ok","service":"lucent-api"}` |
| `GET /memory/list?tier=<t>&offset=<n>&limit=<n>` | list entries; optional `tier=` server-side filter |
| `GET /memory/retrieve?query=<q>&data_class=<c>&k=<n>&min_score=<s>` | semantic search |
| `GET /memory/recent-decayed?limit=<n>` | top-N by recency-decay score |
| `POST /memory/store` | write — body `{content, data_class, tier, mind_id, source}` |
| `PUT /memory/{id}` | update content / data_class / tags |
| `DELETE /memory/{id}` | delete |
| `GET /graph/query?entity_name=<name>&mind_id=<id>&depth=<n>` | identity lookup |
| `GET /graph/search?text=<q>&limit=<n>` | mention search |
| `GET /graph/raw-properties?name=<n>&mind_id=<a>` | unflattened properties blob (round-trip safe) |
| `GET /graph/data?limit=<n>` | visualization export — flat nodes + edges |
| `POST /graph/upsert` | write node + optional edge (with orphan/disambiguation guards) |
| `POST /graph/upsert-direct` | write node directly (skips orphan/disambiguation guards; identity guard still applies) |

## The live feed (comms)

`comms/dashboard.py` is the hive-wide view a dashboard reads, and it exists
because `stream_session_events` is the wrong shape for one. That stream is
per-session; it publishes **every** harness event unfiltered, `tool_use`
inputs and `tool_result` bodies included; and it drops the oldest entry from
a full queue with no ordering to notice the hole by. A tile speaker gets away
with all three — it filters on arrival and answers to one person. A console
page answering to every account in the hive's user table does not.

So the feed carries assistant **text** only, filtered before the bytes leave;
sequences every block and reports the earliest it still holds, so a discarded
run renders as a visible gap rather than as prose that reads perfectly and is
missing its middle; and tracks *generating* explicitly. Nothing else could:
`sessions.status` is written to 'running' at creation and on a model switch
and never written back, so anything keyed on it reports every session that
ever took a turn as busy. It is hooked into `_publish_session_event`, the one
place the chat path and the terminal tailer meet.

**Two liveness ceilings, because the paths give different evidence.** A chat
turn is bracketed — `user` opens it, `result` closes it — so silence in the
middle is a tool chain working. A terminal turn has no brackets at all:
`publish_pty_text` emits bare assistant blocks and never a `result`, so
silence is the only end-of-turn signal there is and has to be read as one. At
the chat ceiling a pane quiet since breakfast would still be "generating" a
quarter of an hour later, and since columns go to the longest-running
conversations, stale terminals would hold every slot.

`GET /sessions/live` and `GET /sessions/live/text` are **admin**-guarded, not
service-token: they carry conversation prose from every mind, and the service
token is held by every surface bot.

## Context figures

`sessions` carries `context_tokens`, `context_threshold`, `context_window`
and `context_observed_at`, all nullable and all written by the reporting
Mind. Nothing on this side can compute them: the count is summed from the
harness transcript's `message.usage` on the machine running it, and the
threshold is read off that Mind's spawn arguments — the same model caps in
two different places depending on whether the long-context pin was on the
command line, so a gateway deriving it from a model name would be wrong for
half the hive.

Null stays null. A zero draws a conversation sitting at 138k as having its
whole window free, which is the one wrong answer that looks like good news.
Every figure is returned with its age, because it is measured once per
completed turn and is therefore never live.

`POST /sessions/context` takes the Stop hook's own addressing — surface and
conversation, as `/sessions/record-turn` already does — since the hook knows
which conversation it is bound to, not which row the gateway filed it under.
A rotation clears both the stored count and the feed's buffered text: they
belong to a transcript that no longer exists, and left in place they render a
fresh conversation as one already at its rotation point.

## Identity convention

Every write must populate `mind_id` with the **canonical mind id** — for
registry-managed minds this is a UUID issued by the consuming mind's
session manager. Hardcoding short names (`"ada"`, `"bob"`) creates
parallel identities that diverge from the registry. The schema column is
`TEXT`; the database does not enforce the rule. See the consuming side's
docs.

## Bearer auth

`LUCENT_BEARER_TOKEN` / `COMMS_BEARER_TOKEN` env vars on the
containers. Empty value = bypass mode with a startup warning (deployment
safety so a fresh container doesn't lock the operator out before the
token is set). Once set, validation is enforced; unauthenticated
requests get 401.

## Documentation

The full design, implementation playbook, and verifiable requirements
live under [`docs/`](./docs):

- **[Design](./docs/memory-system-design.md)** — mind-agnostic
  architecture: rotation, four-layer bootstrap, capture pipeline,
  pruning, graph query semantics. Read this first.
- **[Implementation](./docs/memory-system-implementation.md)** — adopter
  playbook. How a mind plugs into the shared service: env, identity
  convention, hooks per harness, verification checklist, "Constraints
  (don't relearn)".
- **[Requirements](./docs/memory-system-requirements.md)** — 84
  verifiable requirements across 15 sections, each with a verification
  method.
- **[Session prompt composition](./docs/session-prompt-composition.md)**
  — what `comms/bootstrap_loader.py::compose_prompt_blocks` builds (soul
  + standing + decay-weighted recent + session-memory carry-forward),
  the dispatch payload contract, and how the composed string is shipped
  to each mind's harness.
- **[SMS inbound](./docs/sms-inbound.md)** — the comms SMS surface.

## Pruning

APScheduler in-process, default `0 4 * * * America/Chicago`. Override
via `PRUNE_CRON` and `PRUNE_TIMEZONE`. Per-class strategies live in
`lucent_api/prune_memory.py`:

- `prune_ephemeral()` — no-op.
- `prune_current_state()` — anchor priority: `codebase_ref` →
  `expires_at` → `kg_entity` → decay (180d half-life, 0.02 threshold).
- `prune_future_state()` — Ollama shipped-check, then decay (90d).
- `prune_feedback()` — decay only (90d). Standing-tier exempt.

Run output is appended as JSONL to `/data/prune.log` inside the
container.

## Schema

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mind_id TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding BLOB NOT NULL,
    tags TEXT,
    source TEXT NOT NULL,
    data_class TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'contextual',
    as_of TEXT,
    expires_at TEXT,
    superseded INTEGER DEFAULT 0,
    recurring INTEGER DEFAULT 0,
    codebase_ref TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mind_id TEXT NOT NULL,
    type TEXT NOT NULL,             -- 'Mind' for identity nodes
    name TEXT NOT NULL,
    first_name TEXT, last_name TEXT,
    properties TEXT DEFAULT '{}',
    data_class TEXT, tier TEXT, source TEXT,
    as_of TEXT, created_at REAL, updated_at REAL,
    UNIQUE(mind_id, name)
);

CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mind_id TEXT, source_id INTEGER, target_id INTEGER,
    type TEXT, as_of TEXT, source TEXT,
    data_class TEXT, tier TEXT, created_at REAL
);
```
