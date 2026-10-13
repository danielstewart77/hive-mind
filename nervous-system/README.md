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
| `POST /memory/retrieve` body `{query, data_class, k, min_score}` | semantic search |
| `GET /memory/recent-decayed?limit=<n>` | top-N by recency-decay score |
| `POST /memory/store` | write — body `{content, data_class, tier, mind_id, source}` |
| `PUT /memory/{id}` | update content / data_class / tags |
| `DELETE /memory/{id}` | delete |
| `POST /graph/query` body `{names, depth}` | identity lookup, every name in one request |
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

`sessions` carries `context_tokens`, `context_threshold`, `context_window`,
`context_kind`, `context_has_images`, `context_model` and
`context_observed_at`, all nullable and written by the reporting Mind once a
turn has run. Nothing on this side can measure a turn: the count is the
turn's last model request on the machine running it — the provider's own
figure (`kind` "usage") or the harness's estimate ("estimated") — and the
threshold is the window of the model the turn ran on times that model's
`rotation_threshold_percent`, both written on the inference proxy's row for
the model.

The one figure the gateway writes itself is a fresh conversation's opening
size. Whatever starts a conversation over under a session — a harness
switch, a model handover, a terminal rotation, a chat rotation's successor —
opened it on text the gateway composed, so it records that text's size,
characters over four, plus the harness's own overhead, as `kind` "estimated"
for the new model, with no images, before the first reply. A pane counts the handover it was handed; a
process counts the composed prompt beside it as well. The first report from
a real turn replaces it.

The overhead is what a harness puts in front of a conversation before any of
its text — its system prompt, tool definitions and project instructions — and
it is learned. A conversation's first provider count (`kind` "usage") —
from its first turn, naming the conversation now on the row, and reported
from the surface the conversation was opened in (the hook sends its
`HIVE_SURFACE`: `terminal` is a pane, a chat surface's name a process; a
report naming none teaches nothing) — less the opening it was estimated at,
a resent message included, is stored in `context_overhead` per mind,
harness, model (the row's name for it) and surface, whose harnesses ship
different prompts. That first turn's own work only adds to the difference, so
the least seen stands, and a difference of nothing or less is ignored. Any
other report leaves the opening for one that can teach. Until a mind has a
figure for that harness and surface, the least it has under another model
stands in, then a default per harness taken from the first measured reports
on record after switches onto an empty handover: claude 43,000 tokens, dsh
20,000, codex 9,000, and the largest for a harness not listed.

Every opening is sized around it. The room is the model's threshold, or half
its window where nobody knows the threshold, less the overhead (the larger of
the pane's and the process's for a conversation a pane holds) and any message
the conversation opens with. The composed prompt is trimmed — session memory
first, keeping its tail, then recent memory, keeping its head; the soul is
never trimmed — to at most half the room, and the handover has what the
prompt leaves. That holds for a pane's handover too, though the pane is
handed the handover alone, because a pane can be gone, or adopted by a chat
surface, before the handover is spent. The prompt's size is stored beside
the carry-forward (`carry_forward_prompt_cap`), and a respawn recomposes the
prompt to no more than that. A rotation's seed is sized the same way. A
switch or model handover onto a model with no room for the overhead and the
soul beside the least handover worth sending is refused with 409
(`SwitchWontFit`) before anything moves; `/command` returns its reason as the
error, and an overflow it refuses keeps the message on the ledger. The old figures go and the estimate arrives in one write, so
nothing reads the row between them. A report of `kind` "none" — a hook that
could not read its transcript — measured nothing: a size already recorded
stands with its own age, model and images, and only a row with no size at all
records it.

Null stays null. A zero draws a conversation sitting at 138k as having its
whole window free, which is the one wrong answer that looks like good news.
Every figure is returned with its age, because it is measured once per
completed turn and is therefore never live.

`POST /sessions/context` takes the Stop hook's own addressing — surface and
conversation, as `/sessions/record-turn` already does — since the hook knows
which conversation it is bound to, not which row the gateway filed it under.
A report naming a `conversation_id` the row no longer runs is dropped: a Stop
hook finishing after a switch or rotation would otherwise write the old
transcript's size over the new one. A rotation clears the stored count and
the feed's buffered text for the same reason, and records the new
conversation's opening estimate in its place; the threshold stays, since the
model is the same. A chat rotation's successor inherits its predecessor's
window and threshold when it runs the same model. A rotation armed on a
conversation a pane holds (`terminal_held`, or a terminal owner) is staged,
whichever process's hook armed it — the chat process that answers the
console is never told it is a pane — and only the pane's own fire swaps it;
`send_message` never finalizes such a row into a successor, and its chat
turns go on to the current conversation until the pane fires. A mind's
`rotate-pty` answer of `{"rotated": false}` is the only one read as "no
pane"; any other refusal raises, so a caller never ends a pane that may be
running.

`GET /sessions/live` returns the producing conversations under `sessions` and
every other open one — bound to a surface, neither closed nor suspended —
under `idle`, each with the same `context` reading.

## A conversation always fits its model

`switch_model` resumes the conversation under its own id only when the new
model can hold it: its last measured size at or under the new model's
threshold (the `rotation_threshold_percent` and `context_window` on that
model's row of the Mind's model listing, which relays the proxy's; a row
without a percentage falls back to the ratio the conversation's own last
report implied), and no images on a model whose
listing says it takes none or does not say. A conversation just started over
is judged on its opening estimate like any other size, so a model change
straight after a harness switch stays in place when the handover fits. A
size nobody measured, on a conversation that has had turns, is never assumed
to fit. Otherwise the
conversation is handed over, the way a harness switch hands one over: the
Mind renders it through `/handover`, sized so the handover, the composed
prompt and the next message sit under the new threshold, and the same row
opens a fresh conversation id on it. A live pane is respawned onto it in
place; anything else gets a process on it, never a new id with nothing
running it. The armed rotation, the stored carry-forward and the Mind's
on-disk marker (`DELETE /sessions/{id}/rotation-marker`) go with the old
conversation. The answer carries a `warning` saying why, and the row takes
the new model's window and threshold at once either way.

A turn whose result carries `error_code: CONTEXT_WINDOW_EXCEEDED` is replaced
the same way, at once, with the user turn taken back out of the ledger and a
`<failed-turn>` note listing the tool calls that attempt completed — or, for a
harness that reports only a tally, how many — and the same message, with its
images and `goal_rounds`, is sent again on the fresh conversation inside the
same turn lock, so nothing typed meanwhile overtakes it. A row that never
reported a window is sized from the model's listing. That pending re-send is
held on the row (`resend_content`, `resend_images`, `resend_goal_rounds`)
until it goes out, so a restart in between sends it at start-up; it is
stamped when dispatched, so a restart mid-answer does not send it twice, and
cleared when the Mind answers, error included. A re-send that overflows again
is reported and stops. A goal run that ends with `stop_reason:
rotation_needed` and its `goal` (objective, `roundsLeft`) is replaced once
and the objective dispatched again with `goal_rounds` set to the rounds it
had left. A rotation's composed prompt is trimmed — session memory first,
keeping its tail — to the threshold of the conversation it replaces.

Every replacement dates the conversation (`conversation_started_at`). A
rotation arm naming no conversation is refused when the conversation began
after the hook did (`started_at`), or, with no start given, within the
longest a composition runs. A reported window or threshold of zero is
ignored rather than written.

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
