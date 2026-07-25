# Memory System Implementation

> Adopter's playbook. How a mind plugs into the shared memory system.
> Source of truth for design: `memory-system-design.md`. Verifiable
> requirements: `memory-system-requirements.md`.

---

## Architecture

The memory system is a containerized service —
**hive-lucent** (built from `nervous-system/`) — that hosts:

- **lucent_api** (FastAPI): vector store + KG, all routes bearer-gated.
- **APScheduler** (in-process): runs the daily `prune-memory` cron at 4am.
- **lucent.db** (SQLite, mounted volume): one shared database for every
  mind that connects.

Every mind talks to the container over HTTP with a bearer token. Minds
themselves hold no lucent code — only a `.env`, a token, and the hooks
+ scripts that call the API.

```
            ┌────────────────────────────────────────┐
            │  hive-lucent  (container)              │
            │  ┌──────────────────────────────────┐  │
            │  │  lucent_api (FastAPI + bearer)    │  │
            │  │  /memory/{store,retrieve,list,...}│  │
            │  │  /graph/{query,upsert,search,...} │  │
            │  └──────────────────────────────────┘  │
            │  ┌──────────────────────────────────┐  │
            │  │  APScheduler                      │  │
            │  │  - prune-memory @ 0 4 * * *       │  │
            │  └──────────────────────────────────┘  │
            │  └─ /data/lucent.db  (mounted volume) │
            └─────────────────▲──────────────────────┘
                              │
                  HTTP + Bearer (LUCENT_BEARER_TOKEN)
                              │
        ┌─────────┬───────────┴───────────┬─────────┐
        │ mind A  │ mind B  │ mind C  │ ... │
        │ hooks + │ hooks + │ hooks + │     │
        │ scripts │ scripts │ scripts │     │
        │ + .env  │ + .env  │ + .env  │     │
        └─────────┴─────────┴─────────┴─────┘
```

---

## Part I — The shared nervous system

What it provides. Adopters consume; they don't rebuild.

### Repo

```
~/Storage/Dev/hive-mind/nervous-system/
├── lucent_api/                # FastAPI app + routers + lucent_graph + lucent_memory
│                              # + memory_schema, prune_memory, secrets, notify_utils
├── comms/                     # gateway — sessions, broker, bootstrap, HITL
├── server.py                  # lucent container entry — starts uvicorn + APScheduler
├── Dockerfile                 # lucent image (comms has comms/Dockerfile)
├── data/                      # (volume) lucent.db, broker.db, sessions.db, prune.log
├── scripts/migrations/        # one-shot DB migrations (archived)
└── .env                       # LUCENT_BEARER_TOKEN, COMMS_BEARER_TOKEN
```

The `lucent` and `comms` compose services live in the repo-root
`docker-compose.yml`.

### Endpoints

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
| `POST /graph/upsert` | write node + optional edge — **schema-validated** (`validate_node` / `validate_edge` from `schema_registry`) ahead of the orphan/disambiguation guards. Returns `{ok: bool, code?, detail?, ...}` |
| `POST /graph/upsert-backup` | legacy un-validated handler, parked during the schema-enforcement cutover. Returns `{upserted: bool, reason?: ...}`. Delete once stable. |
| `POST /graph/upsert-direct` | write node directly (skips orphan/disambiguation guards; identity guard still applies) |

### Database schema

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

### Bearer auth

`LUCENT_BEARER_TOKEN` env var on the container. FastAPI dependency
applied to every router except `/health`:

```python
def require_bearer(authorization: str = Header(default="")) -> None:
    expected = os.environ.get("LUCENT_BEARER_TOKEN", "")
    if not expected:
        return  # bypass mode (warning logged at startup)
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    if authorization[7:] != expected:
        raise HTTPException(401, "Invalid token")
```

Empty token = bypass with warning. Deployment safety so a fresh
container doesn't lock the operator out before the token is set.

### Cron

APScheduler in-process. Default: `0 4 * * * America/Chicago`. Override
via `PRUNE_CRON` and `PRUNE_TIMEZONE` env vars. Run output appended as
JSONL to `/data/prune.log`.

```python
scheduler.add_job(
    _run_prune,
    CronTrigger(...),
    id="prune-memory",
)
```

`_run_prune` calls `lucent_api.prune_memory.run_all()`, which runs four
per-class pruners (one per data class). See design.md § Pruning for
strategy details.

### Per-class pruners

Implemented in `lucent_api/prune_memory.py`. Four functions, one per class:

- `prune_ephemeral()` — no-op.
- `prune_current_state()` — anchor priority: `codebase_ref` →
  `expires_at` → `kg_entity` → decay (180d half-life, 0.02 threshold).
- `prune_future_state()` — Ollama shipped-check, then decay (90d).
- `prune_feedback()` — decay only (90d). Standing-tier exempt.

Reads entries directly from sqlite (the API doesn't expose all anchor
columns). Writes via API (`DELETE /memory/{id}`).

---

## Part II — Mind integration

Everything an adopter mind needs to do. All minds run as containers on
the shared `hivemind` Docker network — the host-side and bare-metal
patterns have been retired.

### Per-harness coverage

The hook architecture is the same for every harness. What differs is
*how* the harness loads the hooks.

| Harness | Hook config file | SessionStart | Stop | UserPromptSubmit | Notes |
|---|---|---|---|---|---|
| **Claude CLI** (Ada, Bob) | `<mind-root>/.claude/settings.json` | ✓ | ✓ | ✓ | Native — no extra wiring. |
| **Claude SDK** (Bilby) | `<mind-root>/.claude/settings.json` | ✓ | ✓ | ✓ | Loads settings hooks only when the SDK is initialised with `setting_sources=["project"]` (`ClaudeAgentOptions`) or the equivalent on `ClaudeCodeOptions`. SessionStart in Python is *only* available as a shell-command hook, not as a Python callback. |
| **Codex CLI** (Nagatha) | `<mind-root>/.codex/config.toml` (or `hooks.json`) | ✓ | ✓ | ✓ | Same JSON output schema (`systemMessage`, `continue`, `suppressOutput`, `stopReason`). Stable as of v0.124 (April 2026). |

All four hook events emit the same JSON schema on stdout, so the bash
hook scripts are **identical** across harnesses — only the registration
file format changes.

### Identity convention

`mind_id` is a provenance column in lucent (memories, nodes, edges) — it
does not gate reads, but it does identify which mind wrote each row.
**Two values are not interchangeable** even though both are stored as
`TEXT`:

| Concept | Variable | Example | Purpose |
|---|---|---|---|
| Short name | `MIND_ID` | `ada`, `bob`, `bilby`, `nagatha` | Human-readable label for logs, hook output dirs, container names. **Never written to lucent.** |
| Canonical id | `MIND_ID` | `565e5a66-d20c-4266-872a-3268c4c894fc` (a UUID for registry-managed minds) or a literal string for unmanaged minds (currently `"skippy"`) | The value used in every `mind_id` field — `/memory/store`, `/graph/upsert`, `/graph/query?mind_id=…`, all SQL provenance columns. |

Why two: `comms/sessions.py` issues a UUID when a session is created for a
mind the registry knows about. The registry is the source of truth for
the canonical id, not the short name. Hardcoding `mind_id="ada"` writes
a different identity than the registry's `mind_id="565e5a66-…"`, and
the resulting rows look like a different mind to every later read,
prune, and overflow check.

Lookup the canonical value once per mind:

```sql
SELECT mind_id FROM nodes WHERE type='Mind' AND name='Ada';
```

That value goes in compose env as `MIND_ID` and propagates from
there to every hook, skill, and Python caller in the mind container.

A mind that has no registry entry (Skippy today) uses a stable literal
string equal to its short name. Document the choice in the mind's
runtime.yaml/compose so it doesn't drift.

### Step 1 — Get a bearer token

Operator-issued. Phase 1 model is one shared `LUCENT_BEARER_TOKEN`
across all minds. (Per-mind tokens via a `mind_tokens` table is a
follow-on; not required for adoption.)

### Step 2 — Set the mind's env

Per-mind env lives in `<mind-root>/.env` (mode 600) for host-side
reference and is passed into the container via `env_file:` or
`environment:` in compose. Required keys:

```
# Identity (see § Identity convention above)
MIND_ID=<short name>                     # human-readable: ada, bob, bilby, nagatha
MIND_ID=<canonical id>             # UUID from registry (or literal string for unmanaged minds)

# Reach the shared nervous system (joined onto the hivemind docker network)
LUCENT_URL=http://hive-lucent:8424
LUCENT_BEARER_TOKEN=<token>

# Reach hive-tools (Ollama classifier) on the same network
HIVE_TOOLS_URL=http://hive-tools:9421
HIVE_TOOLS_TOKEN=<token>

# Telegram bot (if applicable for this mind)
TELEGRAM_BOT_TOKEN=<token>

# Stop hook + log dirs (inside the container)
AUTO_REMEMBER_LOG_DIR=/usr/src/app/minds/<MIND_ID>/data/auto-remember

# Optional rotation overrides (defaults baked into rotation_check.sh)
ROTATION_TOKEN_THRESHOLD=300000          # 30% of 1M context
CHARS_PER_TOKEN=4
```

Hooks read `MIND_ID` (not `MIND_ID`) when populating any
`mind_id=…` field on the lucent API. `MIND_ID` is for log paths and
display only.

The mind container must be attached to the `hivemind` external network
so `hive-lucent:8424` and `hive-tools:9421` resolve.

### Step 3 — Per-mind directory

```
minds/<mind>/
├── container/compose.yaml           # service definition (joined to hivemind network)
├── .claude/                         # for Claude CLI / SDK minds
│   ├── settings.json                #   declares hooks
│   └── hooks/                       #   the four hook scripts (or symlinks to a shared location)
│       ├── auto_remember.sh
│       ├── session_start_bootstrap.sh
│       ├── contextual_retrieval.sh
│       └── rotation_check.sh
├── .codex/                          # for Codex CLI minds (instead of .claude)
│   ├── config.toml                  #   declares hooks
│   └── hooks/                       #   identical scripts
│       ├── auto_remember.sh
│       ├── session_start_bootstrap.sh
│       ├── contextual_retrieval.sh
│       └── rotation_check.sh
├── data/auto-remember/              # capture/soul/rotation/bootstrap logs (volume-mounted)
└── specs/data-classes/              # the 4 spec files (see design.md § Class taxonomy)
    ├── index.md
    ├── ephemeral.md
    ├── current-state.md
    ├── future-state.md
    └── feedback.md
```

Hook scripts can either live inside each mind's directory or be
symlinked to a shared location (e.g. `/usr/src/app/minds/_shared/hooks/`)
so a single source of truth covers all minds. Either pattern works —
the registration file points at an absolute path inside the container.

No `lucent.db` here — the shared container owns the data file.

### Step 4 — Hooks

Pure bash + jq + curl. Each script sources `<mind-root>/.env` at the
top of its detached subshell so every invocation picks up the bearer
token regardless of how the harness was launched.

| Hook | Event | Suppressed when… | What it does |
|---|---|---|---|
| `auto_remember.sh` | Stop | `HIVEMIND_GROUP_SESSION` or `HIVEMIND_SCHEDULED_TASK` is set | Two parallel branches: (A) classify last turn pair via `/ollama/structured`, save to lucent if save-vector verdict; (B) soul self-reflect — read current `soul_values` from KG, ask Ollama for additions, upsert if `update=true`. |
| `session_start_bootstrap.sh` | SessionStart | `HIVEMIND_SCHEDULED_TASK` is set | Build four-layer systemMessage: identity + standing rules + decay-weighted recent + carry-forward. Emit `{"systemMessage": "..."}` to stdout. |
| `contextual_retrieval.sh` | UserPromptSubmit | `HIVEMIND_GROUP_SESSION` is set | Top-3 similarity search against `data_class=feedback`, inject `<behavior-rules>` block on hit. |
| `rotation_check.sh` | Stop | `HIVEMIND_GROUP_SESSION` or `HIVEMIND_SCHEDULED_TASK` is set | Char-count transcript; if estimated tokens ≥ threshold, kill old session and spawn a new one with same `client_ref`. Drops a `rotation-prior-<client_ref>.path` hint file for the new session's bootstrap to pick up the carry-forward. |

The two suppression env vars exist to keep group-chat sessions from
bleeding hook output into the shared SSE stream and to keep
scheduler-driven service calls (e.g. `/check-reminders` every 15 min)
from burning tokens on capture/reflect work that has nothing to
reflect over.

#### Registration — Claude CLI / SDK (`.claude/settings.json`)

```json
{
  "hooks": {
    "Stop": [{"hooks": [
      {"type": "command", "command": "bash /usr/src/app/minds/<mind>/.claude/hooks/auto_remember.sh"},
      {"type": "command", "command": "bash /usr/src/app/minds/<mind>/.claude/hooks/rotation_check.sh"}
    ]}],
    "UserPromptSubmit": [{"hooks": [
      {"type": "command", "command": "bash /usr/src/app/minds/<mind>/.claude/hooks/contextual_retrieval.sh"}
    ]}],
    "SessionStart": [{"hooks": [
      {"type": "command", "command": "bash /usr/src/app/minds/<mind>/.claude/hooks/session_start_bootstrap.sh"}
    ]}]
  },
  "disableAutoCompact": true
}
```

For Claude SDK minds, the SDK's options call must include
`setting_sources=["project"]` so the settings file is loaded — without
it, SessionStart will not fire.

#### Registration — Codex CLI (`.codex/config.toml`)

```toml
[features]
codex_hooks = true

[[hooks.SessionStart]]
matcher = "startup|resume"

[[hooks.SessionStart.hooks]]
type = "command"
command = "bash /usr/src/app/minds/<mind>/.codex/hooks/session_start_bootstrap.sh"
timeout = 10

[[hooks.UserPromptSubmit]]

[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "bash /usr/src/app/minds/<mind>/.codex/hooks/contextual_retrieval.sh"

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "bash /usr/src/app/minds/<mind>/.codex/hooks/auto_remember.sh"

[[hooks.Stop.hooks]]
type = "command"
command = "bash /usr/src/app/minds/<mind>/.codex/hooks/rotation_check.sh"
```

#### Migrating from `/self-reflect`

Minds that previously used the `/self-reflect` paradigm (an interior
`claude -p '/self-reflect --load|--reflect'` invocation triggered by a
shell hook) need the following deletions before the new architecture
takes over:

1. **Delete** `<mind-root>/.claude/hooks/session_start_identity.sh` (replaced by `session_start_bootstrap.sh`).
2. **Delete** the old `<mind-root>/.claude/hooks/soul_nudge.sh` (replaced by the soul-reflect branch inside `auto_remember.sh`).
3. **Remove** the `Stop`/`SessionStart` registrations that pointed at the deleted scripts in `settings.json`/`config.toml`.
4. **Add** the four new registrations from the block above.

The `/self-reflect` slash command itself can stay in place for manual
invocation if desired — it just stops being driven by hooks.

### Step 5 — Scripts and skills

Bash scripts (under `~/.claude/scripts/`):

- `remember.sh` — `/remember` backend. Classifies + saves with `tier=contextual, source=user`.
- `always_remember.sh` — `/always-remember` backend. Direct standing-tier write with `data_class=feedback, tier=standing, source=always-remember`.

Skills (under `~/.claude/skills/`):

- `remember/` — invokes `remember.sh` with stdin chunk.
- `always-remember/` — invokes `always_remember.sh` with stdin chunk.
- `prune-memory/` — invokes `lucent_api.prune_memory.run_all()` (when run inside the container; outside, an HTTP-only variant).
- `memory/` — operational utility for browsing the store.

### Step 6 — Identity node

The mind needs an identity node in the shared KG. The node's `mind_id`
must be the canonical id (`MIND_ID` — see § Identity convention),
not the short name. First-time setup:

```sql
-- Run inside the shared container or via a one-shot script.
-- :MIND_ID is the registry-issued UUID (or the literal short
-- name for unmanaged minds — currently 'skippy').
INSERT INTO nodes (mind_id, type, name, properties, data_class, tier, source, created_at, updated_at)
VALUES (:MIND_ID, 'Mind', '<MindName>',
        json_object('soul_values', json_array('<bullet 1>', '<bullet 2>', ...)),
        'current-state', 'contextual', 'user',
        strftime('%s','now'), strftime('%s','now'));
```

Or — if there's an existing soul file — use the same shape and seed via
a small Python script that resolves `MIND_ID` from the registry
first. The identity-node guard (`_check_identity_guard` in
`lucent_graph.py`) keys on `type='Mind'`, so this matters: if you write
the node as `type='Person'`, no guard.

Verify the seed by name and capture the canonical id back, so callers
that need `MIND_ID` can pull it from a single trusted source:

```sql
SELECT mind_id FROM nodes WHERE type='Mind' AND name='<MindName>';
```

### Step 7 — Verify

See § Verification checklist below.

---

## Part III — Verification checklist

Before declaring a mind successfully integrated:

1. **Service health** — container's `/health` returns OK.
2. **Bearer enforcement** — unauthenticated `/memory/list` returns 401; valid bearer returns 200.
3. **Bootstrap** — start a fresh session; `data/auto-remember/bootstrap.log` shows non-zero `identity` (KG soul_values), `standing` (feedback@standing), and `recent` (decay-weighted) byte counts. Carry-forward is non-zero only after a rotation.
4. **Capture** — substantive turn produces an entry in `data/auto-remember/runs.jsonl` with `status: pass`.
5. **Soul reflect** — same turn produces an entry in `data/auto-remember/soul_updates.jsonl` (most often `status: no-update`). At least one `status: updated` entry seen on a clearly identity-shaping turn.
6. **`/remember`** — produces a lucent entry with `source=user`.
7. **`/always-remember`** — produces a lucent entry with `tier=standing, source=always-remember, data_class=feedback`.
8. **Standing-tier overflow** — at 11+ standing entries, an alert fires once (Telegram + `data/auto-remember/alerts.log`); subsequent writes don't repeat the alert; prune-memory removes the warn state when the count drops back to ≤10.
9. **Auto-compact disabled** — confirmed in `~/.claude/settings.json`.
10. **Rotation** — after enough transcript volume to cross the threshold, a new session spawns with the same `client_ref` and the prior session is killed. New session's bootstrap shows non-zero carry-forward.
11. **Pruning** — daily 4am cron fires; `data/prune.log` (in the container's volume) gets a fresh JSONL entry with per-class outcomes.
12. **`graph_query`** — returns identity matches only (no peripheral nodes). `graph_search` returns mention shape `{node_id, node_type, property, snippet}`.

---

## Constraints (don't relearn)

Hard-won lessons. These are durable across minds and worth surfacing
upfront for the next adopter.

### Identity (`mind_id` vs `MIND_ID`)

- `MIND_ID` is the human-readable short name (`ada`, `bob`, `bilby`, `nagatha`). Used for log paths, container names, display.
- `MIND_ID` is the canonical id written to lucent. For registry-managed minds it is a UUID issued by `comms/sessions.py`; for unmanaged minds it is a stable literal string (currently only `"skippy"`).
- **Never write the short name to lucent.** Hardcoding `mind_id="ada"` from a Python caller, a hook, or a SQL seed creates a *second*, parallel mind identity that diverges from the registry. Every later read, prune, and identity-guard check sees a different mind.
- The lucent schema column is `TEXT`; the database does not enforce the convention. This is purely an upstream-discipline issue.
- Resolve once via `SELECT mind_id FROM nodes WHERE type='Mind' AND name='<MindName>'`, then propagate via env (`MIND_ID=…`).

### `lucent /memory/store`

- `source` must be one of `{always-remember, self, session, tool, user}`. Other values return 400. Mapping in use:
  - capture Stop hook → `session`
  - soul self-reflect branch → `self`
  - `/remember` → `user`
  - `/always-remember` → `always-remember`
- `tier=standing` requires `source=always-remember` (server-enforced).
- Returns one of three response shapes — handle all three:
  - `{"id": <new-id>, ...}` — fresh insert.
  - `{"deduped": true, "existing_id": N, "score": 1.0}` — dedup hit (success, just not a new write).
  - `{"stored": false, "error": "..."}` — rejected.

### `lucent /memory/list`

- Caps `limit` at 100 (returns 422 above).
- Use `tier=<tier>` server-side filter to avoid client-side pagination across the full store. Both bootstrap (standing rules) and overflow audit rely on it.
- `mind_id` query param is accepted for backwards compat but ignored — provenance only, not a query filter (REQ-018).

### `lucent /graph/query`

- Identity lookup. Matches case-insensitive on `name`, `first_name`, `last_name`, plus alias substring within the JSON-list `aliases` field.
- Soul values live at `.matches[0].properties.soul_values`, an **array of strings** (join with `\n\n` for display).
- `depth` query param has a minimum of 1, not 0.

### `lucent /graph/upsert`

- **Schema-validated.** Each request runs `validate_node` (type allow-list + property schema + enums) and, when `relation` is set, `validate_edge` (relation allow-list + endpoint-type compatibility) from `lucent_api/schema_registry.py` before the existing orphan / disambiguation guards.
- Response envelope: `{"ok": true, ...write_result}` on success; on rejection `{"ok": false, "code": <error_code>, "detail": <reason>, ...}`. Error codes: `unknown_field`, `unknown_type`, `missing_target_type` (relation set without `target_type`), `unknown_edge`, `endpoint_mismatch`, `orphan_blocked`, `disambiguation_required` (with `similar_nodes`), `write_error`, `internal_error`. Schema rejections also carry `request_new_via: "POST /schema/request-new-type"` so the caller knows the escalation path.
- **Full-replace** semantics on the `properties` JSON blob (`ON CONFLICT(mind_id, name) DO UPDATE SET properties = excluded.properties, ...`) — validation does not change this; anything not passed back is still wiped.
- The `properties` field on the request body is a **string-encoded JSON**, not an object. The server `json.loads()` it.
- The API flattens column values over inner-blob keys when reading. To preserve unrelated keys when round-tripping, prefer `POST /graph/properties/merge` (additive) or read the raw blob via direct sqlite, augment, write back the full string.
- **Never smoke-test with `properties: "{}"` against an existing node** — empty blob wipes everything (soul_values included). Always pass the full existing blob plus your changes.
- `POST /graph/upsert-backup` is the legacy un-validated handler from before the cutover, parked as a temporary fallback. It returns the old `{"upserted": bool, "reason": ...}` shape. Delete once stability is confirmed.

### Ollama / hive-tools

- `gpt-oss:20b-32k` structured outputs only work via Ollama `/api/chat` (the two-request thinking-model dance). `/api/generate` returns empty strings when `format` is set on a thinking model. The hive-tools wrapper at `tools/ollama.py` correctly uses `/api/chat`.
- Hive-tools requires `Authorization: Bearer ${HIVE_TOOLS_TOKEN}`. Same bearer pattern as lucent, different token (separate services, separate blast radius).

### Env propagation

- `<mind-root>/.env` (mode 600) is the single source of truth for per-mind config and tokens.
- All hooks source it at the top of their detached subshell. New hooks must follow the same pattern:
  ```bash
  set -a
  . "$MIND_ROOT/.env"
  set +a
  ```
- The `LUCENT_BEARER_TOKEN` and `HIVE_TOOLS_TOKEN` env vars must be present in any process that calls those services. Hooks source them; Python services receive them via `env_file:` in compose.

### Rotation

- Gateway endpoint: `POST /sessions` needs `owner_type, owner_ref, client_ref, mind_id` (+ optional `model`). `DELETE /sessions/{id}` kills.
- `client_ref` lives in `active_sessions` (separate from `sessions`). To get full session metadata: `JOIN sessions s ON ... LEFT JOIN active_sessions a ON s.id = a.session_id`.
- The Claude Code SessionStart event does NOT pass `prior_transcript_path` on a fresh-spawned session. The carry-forward layer is wired via a one-shot hint file at `data/auto-remember/rotation-prior-<client_ref>.path` written by `rotation_check.sh` and consumed by `session_start_bootstrap.sh` (≤60s old; deleted after read).
- Threshold defaults: `ROTATION_TOKEN_THRESHOLD=300000` (30% of 1M context for Opus 4.7 / Sonnet 4.6), `CHARS_PER_TOKEN=4`.

### Identity-node guard

- `_check_identity_guard` keys on `type='Mind'`. A mind whose node is `type='Person'` or `type='Agent'` is **not protected** by the guard.
- When seeding a mind's identity node for the first time, write `type='Mind'` from the start.
- The valid-entity-type set is derived from `schema_registry.SCHEMA_TYPES` (single source of truth) and includes `Mind`. Don't drop it.

### Class taxonomy

- The classifier loads every spec under `specs/data-classes/` at runtime and builds the enum from the directory listing. **No class names are hardcoded** in hooks or scripts. Adding a new class = writing the spec; no code change.
- Old class names (`technical-config`, `ada-behavior-rule`, etc.) may exist in legacy entries. They're readable but not classifier targets. Pruners skip entries whose class isn't in the current four.
