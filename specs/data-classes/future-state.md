# Data Class: future-state

## Description
Planned, intended, or designed things that haven't shipped yet.

Covers:
- Project designs and architecture for systems not yet built.
- Stated intentions ("Daniel plans to do X").
- Roadmap items, prerequisites for future work.

When a planned thing ships, the implementation event is captured as a
fresh `current-state` entry; the obsolete `future-state` entry is
deleted by the pruner. Entries are not reclassified in place.

## Actions
- save-vector
- save-graph

## Pruning
Three checks, run on cadence:

1. **Shipped check** — for each entry, POST to
   `${HIVE_TOOLS_URL}/ollama/structured` with the entry plus recent
   `current-state` entries on the same topic. Schema:
   `{shipped: bool, reason: string}`. If `shipped: true`, delete.
2. **Decay-on-age** — `half_life_days: 90`, `delete_below_score: 0.02`.
3. **Contradiction-on-capture** — when a new chunk supersedes an
   existing future-state entry, similarity-search the top-K neighbours
   and POST to `/ollama/structured` with schema
   `{contradicts: bool, reason: string}`. If true, delete the older
   entry before writing the new one.

- cadence: "0 4 * * *"
