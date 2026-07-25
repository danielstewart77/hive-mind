# Data Class: current-state

## Description
Durable facts about the present state of the system, codebase, people in
Daniel's life, or the minds in the hive.

Covers:
- Code architecture, configuration, file locations, build events.
- People in Daniel's life and their relationships.
- Identity facts about the minds in the hive.
- Scheduled events with a specific datetime (`expires_at` required).

## Actions
- save-vector
- save-graph (when an identifiable entity or relationship is present)

## Optional Anchor Fields
The pruner reads these to decide which strategy to apply. Set whichever
applies to the chunk:
- `codebase_ref` — comma-separated file paths or symbols
  (e.g. `core/sessions.py,SessionManager.send_message`). Use when the
  fact references specific files, classes, or functions.
- `expires_at` — absolute ISO 8601 datetime. Use for scheduled events.
- `kg_entity` — name of the canonical KG node this fact relates to.
  Use when the fact is about a specific person or named entity.

## Pruning
First match wins; fall through if absent:

1. `codebase_ref` set → `verify_codebase_ref`. Verify every path/symbol
   in the comma-separated ref still exists (file check or a repo-wide
   grep for the symbol) — delete if any token no longer resolves. This
   only checks existence, not whether the fact's content still matches
   the code; no re-embedding happens.
2. `expires_at` set → `verify_external`. Delete after the timestamp passes.
3. `kg_entity` — not implemented. There's no `kg_entity` column on
   `memories` and no corresponding pruner branch; a chunk tagged this way
   today just falls through to step 4.
4. No anchor → `decay_only` with `half_life_days: 180`,
   `delete_below_score: 0.02`.

- cadence: "0 4 * * *"
