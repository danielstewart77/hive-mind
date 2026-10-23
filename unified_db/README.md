# The one database

Hive is moving off seven SQLite files onto one Postgres database. This package
holds the decided layout and the tooling that builds it. Nothing here moves
any service onto Postgres: the SQLite stores stay live and untouched until the
cutover story, so this is additive.

## Why

Two reasons, both decided up front. Hive is going into a corporate setting,
where "it runs on five or six SQLite files" is not a sentence that survives
review. And the orchestration being built on top of it — small single-purpose
agents driven by other agents — means many simultaneous writers.

That second reason is not theoretical. `sessions.db` is in `delete` journal
mode rather than WAL, so a *reader* blocks a writer, and comms holds a
read-write lock on it for the life of its container. A one-off write against
it waits out its busy timeout and fails.

## Layout

One database, six schemas. Schemas rather than one flat namespace because the
collisions are real: `credentials` exists three times across the sources
meaning three different things, `users` twice, and health's singular
`conversation` sits beside broker's `conversations`. Renaming tables to dodge
that would rewrite every query for no gain.

| schema | from | holds |
|---|---|---|
| `core` | `broker.db`, `sessions.db`, console identity | minds, sessions, conversations, users, groups, credentials, surface claims |
| `memory` | `lucent.db` | memories, graph nodes and edges, soul changes |
| `board` | console | work items, design sessions |
| `tools` | `hivetools.db` | API tokens, human-in-the-loop approvals |
| `health` | `health.db` | the one context a corporate install does not create |
| `proxy` | `proxy.db` | providers, models, keys, usage metering |

`schema_map.py` is the whole decision in one file. Every table in every source
is named there exactly once, and `unmapped()` is what keeps that honest: a
table nobody has decided about is an error, so a store gaining a table cannot
be quietly left behind by a migration that reports success.

## Files

- `schema_map.py` — the layout: placements, three merges, four drops, and the
  files that are deliberately not stores.
- `build_schema.py` — emits the Postgres DDL, read through `PRAGMA table_info`
  rather than by parsing `CREATE TABLE` text. Anything it cannot translate
  confidently is reported, never guessed and never silently skipped.
- `label_backfill.py` — decides which session row each kept terminal label
  belongs on, for the loader to apply. Not run against the live `sessions.db`;
  see the journal-mode note above.

## Running it

    python -m unified_db.build_schema --notes-only      # translation notes
    python -m unified_db.build_schema | psql ...        # the DDL
    python -m unified_db.label_backfill                 # report, writes nothing

## What is not here yet

Real foreign keys and the not-nulls the file split made impossible, the move
of memory search off FTS5 onto pgvector, the data loader and its row-for-row
verification, and the cutover. Each is its own story under "One database for
all of Hive".
