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

## The column shape

A source column name travelled into the unified schema unexamined, because the
generator reads `PRAGMA table_info` and emits what it finds. `schema_map`
decides the final shape instead, and `build_schema` applies it as the DDL is
emitted rather than in an ALTER pass afterwards — an ALTER pass is a second
place the shape is decided, and the one the generator does not read.

`COLUMN_RENAMES` fixes five names that were wrong about what they hold.
`core.sessions.claude_sid` becomes `conversation_id`, because the gateway mints
that id for claude, codex and dsh alike and the vendor's name on a
harness-neutral thing makes a reader decode it. `owner_type` and `owner_ref`
become `surface` and `surface_id`: they are the kind and the address of the one
endpoint holding the conversation, not a from and a to, and `surface` is the
word the rest of the system already uses. `summary` becomes `opening_line` —
it is the first 100 characters of the first user message, written once and
never updated, and calling it a summary invites someone to write a summarizer
into it. `core.session_memory.session_id` becomes `conversation_id` for the
same reason its `UNKEYED` entry exists: the name is what kept inviting a
foreign key onto a column that holds no session id.

`DROPPED_COLUMNS` carries three that are not worth the trip, each with its
count: `autopilot` (0 of 8,142 rows ever held 1), `rotated_from` (an artifact
of a chat rotation inserting a successor row) and `group_session_id`, which
goes with `core.group_sessions` itself — 0 rows since it was written, and the
two declared relationships that named it. The hive's declared relationship
count falls from 26 to 24.

`TYPE_OVERRIDES` makes `terminal_held` and `context_has_images` real
`boolean`s; they were `bigint` only because SQLite has no boolean and the type
map carried `INTEGER` across. A retyped column's default is translated too,
since `DEFAULT 0` against a `boolean` is a type error Postgres raises at apply
time, inside the one transaction, taking the whole schema down rather than the
one column.

`core.sessions.rotation_armed` is deliberately *not* converted. It looks like
the same thing and is not: 0 is unarmed, 1 is a chat surface finalized into a
successor row, and 2 is a staged terminal only the pane's own fire may swap.
Collapsing it to a boolean destroys a permanent session row, because a staged
terminal read as merely "armed" is retired for a successor instead of being
respawned in place.

A rename is not only a column definition. It follows the primary key, the
UNIQUE constraints, the foreign keys the source declared, and the bodies of
CHECK expressions and standalone indexes — the two places the generator
carries source SQL across verbatim instead of rebuilding it. It is also
reversed on the way *out*: the row counts behind every relationship are taken
against the live SQLite stores, which keep their own names until they are
retired, so a query built from unified names would fail on a renamed column
and be reported as a relationship nobody could count.

Three things refuse the build rather than being carried: a column decision
naming a column no source carries (the same rule as `unmapped()` — a decision
nobody can check is not a decision, and a stale rename means the real column
travels under its old name with nothing saying so), a drop that would take a
primary-key column or a column some uniqueness depends on, and a CHECK or
index naming a dropped column.

The SQLite files keep their own column names until they are retired. The
old-to-new mapping lives only in `schema_map` and in the loader.

## Files

- `schema_map.py` — the layout: placements, no merges, four drops, the
  final column shape, and the files that are deliberately not stores.
- `build_schema.py` — emits the Postgres DDL, read through `PRAGMA table_info`
  rather than by parsing `CREATE TABLE` text. Anything it cannot translate
  confidently is reported, never guessed and never silently skipped.
- `cutover.py` — one command: load, verify, check, and ask.
- `load_data.py` — fills the schemas from a snapshot of the live stores and
  verifies every table's counts and contents against it.
- `preflight.py` — the questions the old files answer, put to the new
  database before the switch, reported per area.
- `postflight.py` — whether the running services are actually reading the new
  database, proved by a marker only it holds.
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

## Searchable text, in place of the virtual table

lucent's `memories_fts` is an FTS5 virtual table that exists on disk and in
no source file, so there is nothing here to translate — the replacement has
to be *declared*. `SEARCH_COLUMNS` declares it: a generated `tsvector`
column with a GIN index, rather than an expression index, so the write path
needs to know nothing about text search and the access layer's drift check
can see a column. It is capped at the first 100,000 characters, because a
tsvector is hard-limited to 1MB and `to_tsvector` over a larger paste raises
— which on a generated column fails the INSERT and loses the memory outright.
A declaration naming a column no source carries refuses the build, the same
rule as `unmapped()`.

`TEXT_CONFIG` is the configuration those columns are built with, and it is
neither `simple` nor stock `english`. FTS5 is configured
`tokenize='porter unicode61'`, so it stems (the live index holds `rotat`,
`restart` and `run` and holds no `rotation`, `restarting` or `running` at
all), it folds diacritics (`jose`, over 4,097 rows carrying non-ASCII
bytes), and it keeps every stopword while lucent filters its own stoplist out
of the query. So the configuration is the snowball stemmer with no stopword
file, behind `unaccent` — which is a trusted extension, unlike `vector`.
Emitted before any table, because a generated column referring to a
configuration the file never creates fails at apply time inside the one
transaction and takes the whole schema with it.

`memories.embedding` is `vector(4096)` through `TYPE_OVERRIDES`. The
dimension is declared rather than left open, and there is no approximate
index to build at that width: pgvector caps HNSW and IVFFlat at 2000
dimensions and `halfvec` at 4000. Note for the loader: there is no cast from
`bytea` to `vector` in **either** direction, so this is a conversion program
rather than an ALTER, and the rollback is restoring the column from a copy
rather than casting back. `USING v::text::bytea` yields the ASCII bytes of
the literal, which `np.frombuffer` accepts without raising whenever the
length happens to divide by four.

## Loading the data, and checking it

One command does the whole thing and ends on the only sentence that matters:

    python -m unified_db.cutover --base /home/daniel/Storage

It snapshots, loads, verifies every table row for row, keeps the snapshot,
runs the pre-switch check against that same copy, and then either asks
whether to switch over or says what stands in the way of asking. The load and
the check are also their own commands, but "both of them are green" was
otherwise a state no code observed:

    python -m unified_db.load_data --base /home/daniel/Storage
    python -m unified_db.load_data --verify-only --snapshot <dir>
    python -m unified_db.preflight --snapshot <dir>

The snapshot is **kept** by default and its path is printed, because the
pre-switch check reads the same copy; `--discard` removes it. Verification
against a *fresh* snapshot of a live store is refused — it would compare the
database against a store that has been written to since and report every
table the hive is busiest in as corrupt.

### The load

Each source is copied through SQLite's own backup API rather than `cp`: a
file copy of a database a service is writing to is a torn page, and the tear
presents as a corrupt snapshot halfway through the load. So a load never
contends with a running service and a bad load is a retry rather than an
outage. The final load before the cutover is the same command with the
services stopped.

Every table is emptied before it is filled, which is what makes a second run
leave the same database as the first rather than a doubled one. It is one
`TRUNCATE` naming every table — how Postgres allows it despite the keys
between them — and without `CASCADE`, because a key reaching in from a table
this loader does not own should stop the run rather than be followed. The
insert order is read off the live foreign keys, parents first, so a key added
to the schema later cannot make the load fail halfway on an order nobody
thought to update.

The identity counters move inside the same transaction as the rows. Each
surrogate key is `GENERATED BY DEFAULT AS IDENTITY` so the loader can insert
the ids it was given, and a counter does not follow an explicit insert —
without `setval` the first application insert after the cutover collides on
the primary key. Outside the transaction, a loader killed between the commit
and the last `setval` would leave a fully committed database with counters
still at 1, which is that same collision arrived at by a crash.

### Four ways a snapshot is not its store

Checked on a copy this run took **and** on one a previous run kept, because
the second is the path the operator actually uses: the loader is re-run after
the pre-switch check has written into the database. A reuse path that checked
only that the file exists is how a zero-byte `broker.db` loaded thirty-three
tables, left seven holding the previous load's rows, and reported clean.

A source is refused when it is absent, when it is too small to be a database
(SQLite opens a truncated file as a valid empty one), when it opens but
reports no tables, and when it holds a table the map says nothing about. The
mirror of that last one is refused too and is the quietest: a table the map
*places* and the file no longer carries would simply be skipped by the plan,
leaving its loaded rows behind with nothing on the report.

One level down, a source **column** with no target is refused. That was the
quietest gap of all: the load asks whether the source carries every target
column and never the reverse, and verification builds its comparison from the
target's columns — so a column added to a store after the DDL was generated
is dropped by the load and outside the comparison by construction. A
`work_items` column holding 447 rows of real data was discarded under a
40-of-40 green report. The DDL is generated at one commit and the load runs
at another, against six live and actively developed services, so this is the
ordinary case.

### Verification is counts *and* contents

A load that wrote the right number of rows with a column shifted by one is a
successful-looking load and a count cannot tell. Every value read back out of
Postgres is compared against the source value converted the way the load
converted it. Both sides are ordered by the primary key with `COLLATE "C"` on
text columns: SQLite compares bytes while Postgres sorts under the database's
own collation, which puts `Organization` before `OS` and ignores the hyphens
inside a conversation id — two correct orderings that differ, which makes
every row after the first divergence read as a mismatch.

A conversion that loses something is named on the report rather than being
silently blessed by a verification that uses the same conversion.
`memories.created_at` is declared `INTEGER` and holds a float on 53 of its
rows; the declaration is what every reader of it means, so the fractional
part goes rather than the row, and the column is on the report.

An embedding is checked against the column's declared width and for finite
values. Its element *type* cannot be checked: 2,048 float64s and 4,096
float32s are the same 16,384 bytes, so a blob of the wrong type would decode
into a plausible vector of the wrong numbers and verification would run the
same bytes through the same conversion and call it a match. The protection is
that every live blob is float32 at exactly the declared width, and that a
store writing anything else would be a change to lucent rather than a
surprise in the data.

### What a green report does and does not mean

It is true of the moment the snapshot was taken. So the live stores are
counted again at the end and any table that has moved is named as **drift**.
On a rehearsal drift is expected, and it is the measure of how much the final
load still has to catch. On the final load, with the services stopped, it has
to be nothing — and `cutover` refuses to ask the switch question while there
is any.

`health.db` is not opened, not copied and not verified, and the empty
`health` schema Postgres carries is left as it is.

## Before the switch, and after

Two checks, because they answer different questions and only one of them can
be asked before the cutover.

`preflight.py` asks the loaded database the questions the SQLite files answer
today — the gateway's mind registry, the session picker including its
`ORDER BY`, memory scoping and the exact bytes of 25 embeddings, the
searchable column, a graph walk, the board's tree, the approval queue, the
proxy's metering, every foreign key — and puts each question to both engines,
reporting per area with what differed.

It also inserts one row and rolls it back, because a counter still sitting
behind its rows is invisible to every read and only an insert can see it. The
counters themselves are **read**, never touched: `pg_sequence_last_value`
answers the question without consuming anything. An earlier version called
`nextval` and put the counter back with `setval(max(id))`, which meant the
check repaired the very fault it reported — the first run failed, the second
passed with nothing changed in between, and the whole thing read like flaky
infrastructure. It also *lowered* a counter legitimately ahead of its rows,
and these tables are mostly holes: `core.messages.recipient_session_id` and
`board.capability_node_settings.node_id` are documented as holding ids of
rows that are gone, so a reused id makes a stale reference silently resolve
to a brand-new unrelated row. A counter ahead of its rows is correct. Only
one behind them is a fault.

`postflight.py` asks the running hive whether it is actually *on* Postgres,
from outside, through each service's own HTTP surface. A read-only comparison
cannot answer that: the loader made the two stores agree, so a service still
reading SQLite answers correctly. So each check writes a marker into Postgres
and nowhere else, reads the service, and puts the original value back — a
gateway that hands back the old model name is a gateway still on SQLite, and
the report says so in those words.

    python -m unified_db.postflight
    python -m unified_db.postflight --restore

What the probe overwrote goes to a file *before* the write. The three columns
it marks are all `NOT NULL` and load-bearing — a mind whose `model` is a name
the proxy does not list has every spawn refused — and the window is an HTTP
call whose timeout is per-socket-operation rather than a deadline, so a
service dribbling bytes holds it open. Held only in process memory, a kill in
that window leaves a marker in a live column with nothing on disk saying what
the value was. A journal left behind refuses the next run, and `--restore`
puts the value back.

lucent's check stores a memory through the API and looks for that row in
`memory.memories`, then deletes it **through lucent** on every path — from
whichever store it actually went to, since deleting from Postgres alone would
leave the memory behind on exactly the path this check exists to detect. It
reads the response before the database, because a refused write and a write
that went to the wrong store are different sentences with different remedies:
lucent answers a 200 with `stored: false` for a class it discards, and an
earlier version read that as "the service is on the wrong store".

Three states, and `absent` is **not** a pass. An earlier version folded
"not installed", "no data to ask about" and "simply down" into one green
state; three of those have different remedies and one of them is an outage.
A service that does not answer is a failure. The console reports absent
unless a token is configured: its board API is guarded by a signed-in admin
session, so a check outside a browser has no credential to present, and an
unchecked app is reported as unchecked rather than counted as working.
