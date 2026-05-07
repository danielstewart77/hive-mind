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

1. `codebase_ref` set → `verify_codebase_ref`. Verify file/symbol exists
   and the stored fact matches current code; delete or re-embed.
2. `expires_at` set → `verify_external`. Delete after the timestamp passes.
3. `kg_entity` set → `verify_kg_entity`. Re-query the entity; drop entries
   that contradict newer facts on the same entity.
4. No anchor → `decay_only` with `half_life_days: 180`,
   `delete_below_score: 0.02`.

- cadence: "0 4 * * *"
