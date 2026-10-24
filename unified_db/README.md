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
is named there exactly once — all 63, health's 22 enumerated rather than swept
by a prefix rule — and an undecided table **raises**, exiting 2, so a store
gaining a table cannot be left behind by a migration that reports success. A
source reporting no tables raises too: SQLite opens a zero-byte or truncated
file as a valid empty database, and the alternative is a schema missing a
third of itself with a clean exit code.

Three things are deliberately *not* merged, and the reasons are in the map
beside them. `broker.credentials` and `lucent.credentials` look like one
registry kept twice and are not: 12 holders appear in both and none of the 12
share a `token_sha`, because they are separate credentials for the gateway and
for the memory store. `tools.users` holds one row whose `is_admin` has no
counterpart in `core.users`, and both rows are `id = 1` with incompatible
hashes.

## Files

- `schema_map.py` — the layout: placements, three merges, four drops, and the
  files that are deliberately not stores.
- `build_schema.py` — emits the Postgres DDL, read through `PRAGMA table_info`
  rather than by parsing `CREATE TABLE` text. Anything it cannot translate
  confidently is reported, never guessed and never silently skipped.
There is no label backfill. An earlier draft of this package had one, on the
belief that `terminal_labels` held names whose only copy it was. It does not:
its single row is a *superseded* name. `label_migration` walked that
conversation's rotation lineage to a live session the operator had since
renamed, recorded the row as `skipped_named`, and left it. Carrying it forward
would have written a dead name onto a closed row every picker hides.

## Running it

    python -m unified_db.build_schema --notes-only      # notes only
    python -m unified_db.build_schema > schema.sql      # the DDL

Then apply it with `ON_ERROR_STOP` — `psql` exits 0 over a page of errors
otherwise, so a half-applied schema reports as applied:

    docker exec -i hive-postgres psql -U hive -d hive -v ON_ERROR_STOP=1 < schema.sql

The generator exits 0 only when it had nothing to report: 2 if it refused, 1
if it emitted a note or the live data refused a relationship. A recorded
decision — a column named in `UNKEYED`, a default named in
`DROPPED_DEFAULTS` — never moves the exit code, because those never go away
and a code that is always 1 stops being read. Piping it straight into `psql` discards that, which is
why the DDL goes to a file first.

## Relationships

A foreign key could never span two SQLite files, so most of what the hive
relates survived only as a convention in application code. The ones a single
file *could* declare are read back out of it and remapped through
`PLACEMENT`; the ones the split made impossible are declared in
`schema_map.CROSS_FILE`.

Nothing is declared on faith, and the checking is in three parts because
there are three ways a key goes wrong. A key whose two sides name a different
number of columns, or whose target carries no primary key or unique
constraint, refuses the build — Postgres rejects both at *apply* time, inside
the one transaction, which takes the whole schema down rather than the one
key. Six of the keys here reference `minds.mind_id`, whose uniqueness is an
inline UNIQUE while the primary key is `name`, so that check is load-bearing
rather than theoretical. Then every candidate is counted against the live
stores, and one with so much as a single offending row is reported and left
out, because a constraint the data cannot satisfy is a load that fails
halfway. An empty string counts as an offender: SQLite never looked, and
Postgres treats `''` as a value rather than as absence. A count that cannot be
taken at all — a mistyped column, a store a live writer holds locked — also
refuses, because a key dropped for a typo must not read like one dropped for
a reason.

Rows are counted across *every* source that feeds a table, not just the one
whose shape won a merge. The winner decides the columns; it does not decide
whose rows arrive, and the rows that break a key are as likely to be the
loser's.

Columns that look like references and are not are named in
`schema_map.UNKEYED` with the reason, and the build refuses if one of them
names a column that no longer exists — the same rule as `unmapped()`, for the
same reason. A sentinel is the common case: `memories.mind_id` holds
`'shared'` for a memory belonging to the hive rather than to one mind, and
`hitl_requests.caller_mind` holds `'internal'`. Those are domain values, not
rot, and must never become rows in `minds`.

Two source defaults are dropped rather than translated, named in
`DROPPED_DEFAULTS`: `sessions.mind_id` and `group_sessions.moderator_mind_id`
both default to the literal `'ada'` while `minds.mind_id` holds UUIDs, so the
default has only ever been reachable by an insert the new key refuses.

Constraints are emitted as `ALTER TABLE ... ADD CONSTRAINT` after every table
exists. There is no table order that satisfies every reference — `sessions`
references itself and the stores reference each other both ways — and the
whole file is wrapped in one transaction so a statement that fails leaves
nothing behind. A self-reference is `DEFERRABLE INITIALLY DEFERRED`, which is
what lets the loader insert such a table in one pass: its order is per row,
not per table.

## What is not here yet

The move
of memory search off FTS5 onto pgvector — lucent's `memories_fts` is 7,663
rows of search index and the generator names it as not carried rather than
hiding it behind a filter; the data loader and its row-for-row verification,
which must also `setval` every identity sequence or the first insert after
cutover collides on a primary key; and the cutover. Each is its own story under "One database for
all of Hive".
