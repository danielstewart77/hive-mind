# Session Prompt Composition

`hive-comms` owns the construction of every mind's session system prompt.
When a session is created, `comms` assembles four data blocks from
lucent (and from its own `session_memory` table), concatenates them into
a single string, and ships that string to the mind's backend in the
dispatch payload. The mind passes it through to its harness without
modification.

This document describes the contract: what `comms` builds, how it is
shipped, and what each block contains.

## Composition entry point

The composer lives at `comms/bootstrap_loader.py::compose_prompt_blocks`.
`comms/sessions.py::SessionManager.create_session` calls it once per new
session — first session, rotation, or otherwise — and passes the result
into the spawn payload.

The composer is best-effort: every fetch has a short timeout (5s) and
swallows exceptions. A missing block degrades the prompt but never
blocks session spawn.

## The four blocks

The composed prompt is the concatenation of these blocks, in order,
separated by blank lines. Empty blocks are dropped.

| Block | Source | Cap | Wrapping tag |
|---|---|---|---|
| Soul | lucent KG — `Mind` node `soul_values`, scoped by the mind's UUID | no cap | `<soul>…</soul>` |
| Standing rules | lucent vector store — `tier=standing`, union of this mind's UUID and `mind_id="shared"` | 2000 chars (~500 tokens) | `<standing-rules>…</standing-rules>` |
| Decay-weighted recent | lucent vector store — `GET /memory/recent-decayed?mind_id=<uuid>&limit=20` | 6000 chars (~1500 tokens) | `<recent-memory>…</recent-memory>` |
| Session-memory carry-forward | comms's `session_memory` SQLite table — latest row for `(mind_id, client_ref)` | n/a | `<session-memory>…</session-memory>` |

### Soul

The soul block is the mind's identity in its own voice. The composer
issues `GET /graph/query?entity_name=<Mind.capitalize()>&mind_id=<uuid>&depth=1`
against lucent, pulls `properties.soul_values` from the first matching
node, and wraps each entry as a line inside `<soul>…</soul>`.

`soul_values` is a list of first-person statements. They are the
authoritative identity content for the mind. The KG row is updated by
the soul self-reflect branch of the Stop hook (see capture pipeline in
`memory-system-design.md`).

### Standing rules

Standing rules are durable behavioural instructions written through the
`/always-remember` skill. The composer fetches two scopes:

1. `mind_id=<this mind's UUID>` — the mind's own per-self rules.
2. `mind_id="shared"` — universal house-style rules that apply to every
   mind.

Both queries hit `GET /memory/list?mind_id=<scope>&tier=standing&limit=100`.
Results are de-duplicated by entry id and rendered as flat bullets
inside `<standing-rules>…</standing-rules>`.

The standing tier is guarded server-side: any `POST /memory/store` with
`tier=standing` must carry `source=always-remember` or the write is
rejected. The classifier never writes to standing; only the
user-invoked skill does.

### Decay-weighted recent

A top-20 query against the vector store, scored by recency decay over
the mind's own writes. Each row is rendered as a single bulleted line
tagged with its `data_class` and decay score, then wrapped in
`<recent-memory>…</recent-memory>`. The block is truncated to the
6000-char cap on a paragraph boundary if longer.

### Session-memory carry-forward

When a session rotates (token threshold crossed), the Stop hook posts a
carry-forward envelope to `POST /sessions/{sid}/rotation-memory`. The
envelope is stored in comms's local `session_memory` table keyed by
`(mind_id, client_ref)`. On the next session for the same surface, the
composer reads the most recent row for the pair and wraps its
`carry_forward` field as `<session-memory>…</session-memory>`. Pre-B10
rows without the `carry_forward` field fall back to the raw body.

If the envelope carries a non-empty `continuation` list — user turns
accepted after rotation began whose assistant reply never completed
before `/clear` — the composer renders them in a distinct
`<pending-continuation>` block appended after `<session-memory>`. This
is the late-turn handoff: the watermarked merge from comms's durable
`session_turns` ledger (via `GET /sessions/late-turns`, queried by the
rotation Stop hook) ensures input sent during a rotation is answered by
the next session rather than lost. The continuation is single-source —
rendered only here, never duplicated into `carry_forward`.

A first session for a `(mind_id, client_ref)` pair produces no
session-memory block; the layer is empty until the first rotation
writes the first carry-forward row.

## Dispatch contract

`comms/sessions.py::_spawn` POSTs to the mind's backend
`<mind_url>/sessions` with the following payload shape:

```json
{
  "session_id": "<uuid>",
  "model": "<model>",
  "autopilot": false,
  "resume_sid": "<optional>",
  "surface_prompt": "<optional>",
  "allowed_directories": ["…"],
  "mind_name": "<short_name>",
  "client_ref": "<surface_ref>",
  "owner_type": "<owner_type>",
  "owner_ref": "<owner_ref>",
  "system_prompt_blocks": "<composed_string>"
}
```

The `system_prompt_blocks` field carries the entire composed string.
The mind backend reads it from the request body and passes it directly
to its harness:

- **Claude CLI** minds invoke `claude --append-system-prompt
  "$system_prompt_blocks" …`.
- **Codex CLI** minds prepend `system_prompt_blocks` to the surface
  prompt before invoking `codex exec`.

No mind composes any portion of the prompt. The mind is a thin spawner
that translates the dispatch payload into a harness invocation.

## Identity convention

Every lucent call inside the composer uses `mind_id=<UUID>` — the
canonical mind id read from the broker's `minds` table. Short names
(`ada`, `bob`, …) appear only in the `entity_name` field of the
graph-query call, where lucent looks up the `Mind` node by its
capitalized short-name string (e.g., `entity_name=Ada`).

The soul self-reflect branch of the Stop hook writes back to the same
KG node via `POST /graph/properties/merge`, keyed on the mind's UUID,
so the next composition picks up the new `soul_values`.
