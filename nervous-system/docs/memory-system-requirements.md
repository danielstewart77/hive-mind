# Memory System Requirements

Verifiable requirements for the memory system. Source of truth:
`memory-system-design.md`. Each requirement: **REQ-XXX** | statement | verification method.

---

## A. Vector schema and tiering

**REQ-001** | The vector store schema includes the following fields per entry: `id`, `content`, `data_class`, `tier`, `mind_id`, `created_at`, `source`, `embedding`. | Schema inspection.

**REQ-002** | The `tier` field accepts exactly two values: `contextual` and `standing`. | Schema constraint check.

**REQ-003** | All entries written by the classifier auto-capture path default to `tier: contextual`. | Trace classifier output across all triggers; assert no path emits `tier: standing`.

**REQ-004** | Each data class spec at `specs/data-classes/<class>.md` declares: description, action(s), optional anchor fields, pruning strategy, and cadence. The classifier loads every spec at runtime; no class names are hardcoded into hooks or scripts. | File audit + grep for hardcoded class names in hooks.

**REQ-005** | Adding a new data class registers an entry in `specs/data-classes/index.md` and is picked up by the classifier on the next call without any code change. | Smoke test.

---

## B. Class taxonomy

**REQ-006** | Four data classes are defined: `ephemeral`, `current-state`, `future-state`, `feedback`. The classifier evaluates the three storage classes first; chunks that match none fall through to `ephemeral`. | Spec audit + classifier trace.

**REQ-007** | `ephemeral` action is `discard`. No prune strategy. | Spec inspection.

**REQ-008** | `current-state` action is `save-vector` for facts; `save-graph` when an identifiable entity or relationship is present. | Classifier output trace.

**REQ-009** | `current-state` entries support optional anchor fields: `codebase_ref`, `expires_at`, `kg_entity`. | Schema inspection.

**REQ-010** | `current-state` pruner dispatches by anchor priority — first match wins, fall through if absent: `codebase_ref` → `verify_codebase_ref`; `expires_at` → delete after timestamp; `kg_entity` → re-query, drop contradicted; no anchor → `decay_only` (`half_life_days: 180`, `delete_below_score: 0.02`). | Pruner trace against synthetic entries.

**REQ-011** | `future-state` action is `save-vector + save-graph`. | Classifier output trace.

**REQ-012** | `future-state` pruning runs three checks on cadence: shipped check (POST entry + recent `current-state` to `/ollama/structured`, delete on `shipped: true`); decay-on-age (`half_life_days: 90`, `delete_below_score: 0.02`); contradiction-on-capture (delete superseded entry before writing the new one). | Pruner trace against synthetic entries.

**REQ-013** | When a planned `future-state` thing ships, the implementation event is captured as a fresh `current-state` entry; the obsolete `future-state` entry is deleted by the shipped-check pruner. Entries are never reclassified in place. | Trace test across a synthetic ship event.

**REQ-014** | `feedback` action is `save-vector`. | Classifier output trace.

**REQ-015** | `feedback` contextual-tier pruning runs two checks: contradiction-detection at capture (similarity-search top-K, POST to `/ollama/structured` with `{contradicts: bool, reason: string}`, delete the old before writing the new); decay-on-age (`half_life_days: 90`, `delete_below_score: 0.02`). | Pruner trace.

**REQ-016** | `feedback` standing-tier entries skip both pruning paths. They are removed only by user action. | Behavior audit.

**REQ-017** | Lucent enforces `tier=standing` requires `source=always-remember` on the write path. Any other source/tier combo is rejected. | Server-side write test.

---

## C. Mind isolation and access boundaries

**REQ-018** | Vector store reads return entries from any mind regardless of the requesting mind's identity. No `mind_id` filter on reads. | Cross-mind read test.

**REQ-019** | Vector store writes from any mind succeed for any entry, regardless of which mind originally created the entry. | Cross-mind update test.

**REQ-020** | Knowledge graph identity nodes (one per mind) are writable only by the mind they identify. Attempts by other minds to update an identity node fail. | Auth test.

**REQ-021** | Knowledge graph non-identity nodes are readable and writable by any mind. | Cross-mind read/write test.

**REQ-022** | `mind_id` is recorded as provenance metadata on every write but never used as a query filter. | Code inspection.

**REQ-022b** | Every write to lucent (`memories`, `nodes`, `edges`) must use the **canonical mind id** (`MIND_ID`) as `mind_id`. For minds managed by `comms/sessions.py` this is the registry-issued UUID; for unmanaged minds it is a stable literal string documented in the mind's runtime config. The short name (`MIND_ID` — `ada`, `bob`, etc.) **must not** appear in any `mind_id` field. See implementation.md § Identity convention. | Provenance audit; SELECT DISTINCT mind_id sanity check.

---

## D. Session rotation

**REQ-023** | Rotation runs as a Stop hook (`~/.claude/hooks/rotation_check.sh`), pure bash + jq + curl + sqlite3-via-python3. No polled asyncio task, no Python orchestrator. | Hook script inspection.

**REQ-024** | Rotation triggers when the active session's transcript reaches the configured threshold (default: estimated tokens ≥ `ROTATION_TOKEN_THRESHOLD=300000`, with `bytes / CHARS_PER_TOKEN=4` as the proxy — 30% of the 1M context window). | Threshold test.

**REQ-025** | Both `ROTATION_TOKEN_THRESHOLD` and `CHARS_PER_TOKEN` are configurable via env vars, with defaults baked into the hook. | Env-override test.

**REQ-026** | Rotation steps execute in order: (1) spawn new session with same `client_ref` via `POST /sessions`, (2) the new session's SessionStart hook bootstraps the four layers, (3) `DELETE /sessions/{old_id}` kills the prior session. | Trace verification.

**REQ-027** | Rotation fires only on the Stop event of a delivered assistant response. Mid-turn rotation never occurs. | Race-condition test.

**REQ-028** | The new session is created with the same `client_ref` as the prior session. The active_sessions table rebinds the surface to the new session_id immediately on POST. | DB check.

**REQ-029** | The rotation hook does not invoke the capture pipeline. Capture and rotation are independent Stop-hook concerns registered side-by-side. | Code inspection.

**REQ-030** | Auto-compact is disabled in every supported harness via `~/.claude/settings.json: disableAutoCompact: true`. | Config inspection.

---

## E. Bootstrap

**REQ-031** | Every new session bootstraps with four layers, all concatenated into a single systemMessage: identity, standing rules, decay-weighted recent, carry-forward window. | Inspection of injected systemMessage.

**REQ-032** | Identity is loaded from the mind's KG node (`soul_values` field, an array of strings) via `GET /graph/query`. | Trace test.

**REQ-033** | Standing rules are loaded from vector store entries with `tier: standing` (filtered client-side from `GET /memory/list`). Total injected ≤ ~500 tokens (~2000 chars). | Token-count check.

**REQ-034** | Decay-weighted recent memory is loaded as the top-20 entries scored by `score = exp(-(now - created_at) / 14d)` via `GET /memory/recent-decayed`, capped at ~1500 tokens (~6000 chars). | Score-formula and cap test.

**REQ-035** | The carry-forward window contains the last 4–6 turns of the prior session, verbatim. Cap ~2000 tokens (~8000 chars). On a fresh first session it is empty. | Verbatim-content check across rotation.

**REQ-036** | Carry-forward source priority: SessionStart event's `prior_transcript_path` (or `priorTranscriptPath` or `transcript_path`) first; if absent, fall back to a one-shot hint file at `data/auto-remember/rotation-prior-<client_ref>.path` written by `rotation_check.sh`. Hint files are consumed (deleted) after read; only honored if ≤60 seconds old. | Hint-file lifecycle test.

**REQ-037** | A first session for a mind runs layers 1–3; layer 4 (carry-forward) is empty. The first-session and rotation-spawn paths share one bootstrap implementation. | Code review.

**REQ-038** | Bootstrap content is injected as systemMessage, not as user message. | Message-role inspection.

---

## F. Behavioral rules / standing tier

**REQ-039** | Population of `tier: standing` is permitted only via the `/always-remember` skill. The classifier never writes `tier: standing`. | Code inspection of classifier; smoke test of `/always-remember`.

**REQ-040** | Both standing and contextual rules are injected within a single `<behavior-rules>` XML tag containing flat-bullet content. | Format inspection.

**REQ-041** | All retrieval payloads (`<behavior-rules>`, future tags) are one-tag-deep. No nested XML. Structured payloads use JSON inside the tag; unstructured use flat bullets. | Format inspection across payload types.

---

## G. Capture pipeline

**REQ-042** | `/remember <content>` runs the same direct path the Stop hook does — POST to `/ollama/structured` for classification, POST to `/memory/store` for the write — with `tier: contextual, source: user`. No subagent dispatch. | Smoke test.

**REQ-043** | `/always-remember <content>` writes a vector entry directly with `tier: standing, source: always-remember, data_class: feedback`. Skips classification. | Smoke test.

**REQ-044** | The capture Stop hook (`~/.claude/hooks/auto_remember.sh`) is pure bash + jq + curl. Fires on every Stop. No cadence gate. No Claude subprocess. | Hook script inspection.

**REQ-045** | The capture Stop hook detaches all work in a background subshell (`(...) > /dev/null 2>&1 &`) and never blocks the parent Claude process. | Stop-hook timing observation.

**REQ-046** | The Ollama classification endpoint is `${HIVE_TOOLS_URL}/ollama/structured` (schema-constrained). Hive-tools wraps Ollama's `/api/chat` (not `/api/generate`) to support thinking-model two-request behavior for gpt-oss. | Endpoint smoke test against `gpt-oss:20b-32k`.

**REQ-047** | The default model is `gpt-oss:20b-32k`, overridable via `OLLAMA_DEFAULT_MODEL` on the hive-tools service or per-call via the request body's `model` field. | Override test.

**REQ-048** | The Ollama endpoint is callable from every mind via the bearer token in `${HIVE_TOOLS_TOKEN}`. | Cross-mind smoke test.

**REQ-049** | Classification is LLM-driven schema-constrained sampling. No regex pattern matching for class assignment. | Code inspection.

**REQ-050** | Capture output destinations are limited to: vector store (`save-vector`), knowledge graph (`save-graph`), reminder (`notify`), or discard. No other destinations. | Code inspection of the hook's routing branch.

**REQ-051** | Soul updates are written by the Stop hook's parallel self-reflect branch directly to the KG mind node's `soul_values` field, with `source: self`. They are not a route out of the classifier. | Code inspection.

---

## H. Per-turn retrieval

**REQ-052** | UserPromptSubmit hook runs a vector retrieval against `data_class=feedback` on every user turn. | Hook config audit.

**REQ-053** | Retrieval returns top-3 entries with cosine similarity ≥ 0.65. Entries below threshold are excluded. | Threshold test.

**REQ-054** | When at least one entry passes the threshold, results are injected as a `<behavior-rules>` flat-bullet XML block. Below threshold: no injection. | Format and threshold test.

**REQ-055** | The hook runs the retrieval call with a short timeout. The hook itself does not block the turn. | Timing observation.

**REQ-056** | The hook runs unconditionally — there is no model-discretion path to skip the lookup. | Hook config audit.

**REQ-056b** | `GET /memory/list` accepts an optional `tier=<tier>` server-side filter. When present, only entries matching this tier are returned and counted in `total`. Used by the bootstrap loader's standing-tier subset and the standing-tier overflow audit. Avoids client-side pagination across the full store. | Endpoint test.

---

## I. Dedup at save time

**REQ-057** | Before any vector write, a nearest-neighbour query within the same `data_class` is run. If cosine similarity ≥ 0.92 against an existing entry, the write is deduped — server returns `{deduped: true, existing_id: N}`. | Dedup test.

**REQ-058** | Callers of `/memory/store` treat both `{id: N}` (fresh insert) and `{deduped: true, existing_id: N}` as success. | Code inspection of all call sites.

---

## J. Pruning

**REQ-059** | One `prune-memory` skill dispatches by class, reading each spec's strategy. Per-class prune skills are not required. | Code review.

**REQ-060** | The `verify_codebase_ref` strategy walks every entry of the class with `codebase_ref` set, verifies the file/symbol exists, and compares stored content to current code. Outcomes: delete (code missing), re-embed (code changed but salvageable), keep. | Strategy test against synthetic entries.

**REQ-061** | Re-embedding overwrites the existing vector embedding atomically. No history retained. | Re-embed test.

**REQ-062** | The `decay_only` strategy deletes entries whose decay score falls below `delete_below_score`, where `score = exp(-age_days / half_life_days)`. Per-class params come from the spec. | Threshold test.

**REQ-063** | The `verify_external` strategy queries an external referent (timestamp, file path, etc.) and deletes the entry when the referent is gone or expired. | Strategy test.

**REQ-064** | The `shipped_check` strategy POSTs `future-state` entries plus recent `current-state` entries to `/ollama/structured` with schema `{shipped: bool}` and deletes on `true`. | Strategy test.

**REQ-065** | The `contradiction_detection` strategy runs at capture time (not on cadence) for `feedback` and `future-state`. Similarity-search top-K, POST to `/ollama/structured` with `{contradicts: bool, reason: string}`, delete the old entry before writing the new. | Strategy test.

**REQ-066** | No `manual_only` strategy exists. Every class has automatic pruning. | Spec audit.

**REQ-067** | Prune cadences are registered with the scheduler from the data class specs at scheduler startup. | Scheduler integration test.

---

## K. Graph query semantics

**REQ-068** | `GET /graph/query?entity_name=<name>` matches only on identity fields: `name`, `first_name`, `last_name` (exact, case-insensitive); `aliases` (substring match within JSON-list entries). | SQL inspection; query test.

**REQ-069** | `graph_query` does not include a `properties LIKE '%name%'` clause. Identity lookup is deterministic — same query, same result set. | SQL inspection.

**REQ-070** | `graph_query` returns DISTINCT results by `node_id`. A node matched on multiple identity fields appears once. | Query test.

**REQ-071** | Null or empty identity fields are skipped — they do not false-match empty queries. | Edge case test.

**REQ-072** | Partial-name matching on identity fields is not supported (`Dan` does not match `Daniel` unless `Dan` is registered as an alias). | Behavior test.

**REQ-073** | `GET /graph/search?text=<query>&limit=<n>` performs full-text scan across all property strings of all nodes. | Endpoint test.

**REQ-074** | `graph_search` returns a list of `{node_id, node_type, property, snippet}` objects. | Response shape test.

**REQ-075** | `graph_upsert` is full-replace on the `properties` JSON blob. Callers wanting to preserve unrelated keys must read the current blob, augment, and write back. | Code inspection.

---

## L. Skill self-announcement

**REQ-076** | Every memory skill (`/remember`, `/always-remember`, `/prune-memory`) announces itself on invocation. The announcement appears as the first line of the skill's response in the form `using skill: <skill-name>`. | Output inspection.

**REQ-077** | Nested skill invocations announce in chain (e.g., a top-level skill that invokes a sub-skill prints the parent's announcement, then the child's). | Trace test.

---

## M. Bearer auth (consolidated nervous system)

**REQ-078** | Lucent-api requires `Authorization: Bearer <token>` on every endpoint except `/health`. Empty/unset `LUCENT_BEARER_TOKEN` env var puts the service in bypass mode with a startup warning logged (deployment safety). Once set, validation is enforced and unauthenticated requests get 401. | Unauthenticated request returns 401.

**REQ-079** | Hive-tools requires the same bearer token pattern. `/ollama/structured` and `/ollama/chat` are gated by `hitl_gate("ollama_structured")` and `hitl_gate("ollama_chat")` (both `mode=off` — read-only side effects, no approval needed). | Unauthenticated request returns 401.

**REQ-080** | Each mind holds a bearer token in its `<mind-root>/.env` (mode 600). Phase 1 model: a single shared `LUCENT_BEARER_TOKEN` for the consolidated nervous system. Per-mind tokens with a server-side `mind_tokens` table are a follow-on once cross-mind audit and revocation become operationally important. | Token-from-env override test; revoke test (V2).

---

## N. Phase gates

**REQ-081** | Phase 1 (first mind end-to-end) is complete only when REQ-001 through REQ-080 are implemented and pass verification on that mind. | Phase 1 gate review.

**REQ-082** | Phase 2+ (subsequent minds) requires re-verification of REQ-018 through REQ-077 on each new mind. Mind-specific adjustments documented only where harness contracts differ. | Per-mind gate review.

---

## Coverage map

| Component | Requirements |
|---|---|
| Vector schema & tiering | A: REQ-001 through REQ-005 |
| Class taxonomy | B: REQ-006 through REQ-017 |
| Mind isolation | C: REQ-018 through REQ-022 |
| Session rotation | D: REQ-023 through REQ-030 |
| Bootstrap | E: REQ-031 through REQ-038 |
| Behavioral rules / standing tier | F: REQ-039 through REQ-041 |
| Capture pipeline | G: REQ-042 through REQ-051 |
| Per-turn retrieval | H: REQ-052 through REQ-056b |
| Dedup | I: REQ-057 through REQ-058 |
| Pruning | J: REQ-059 through REQ-067 |
| Graph query | K: REQ-068 through REQ-075 |
| Skill self-announcement | L: REQ-076 through REQ-077 |
| Bearer auth | M: REQ-078 through REQ-080 |
| Phase gates | N: REQ-081 through REQ-082 |

Total: 84 requirements.
