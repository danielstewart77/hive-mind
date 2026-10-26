# hive-data

The one async data-access layer every hive service goes through.

No service opens a database. A service calls a named operation, and where the
database actually lives is one configuration value in that process's
environment — so pointing the hive at a different database is an environment
change rather than an edit to anything.

Every mind and every service tracks `main`. There are no pinned versions to
carry per install, which makes the suite here a gate rather than an oracle:
nothing merges red, because `main` is what every service in the hive is
running.

## Install

```
pip install "hive-data @ git+https://github.com/danielstewart77/hive-data.git@main"
```

Upgrading needs `--force-reinstall --no-deps`, then a second pass over your
own requirements:

```
pip install --force-reinstall --no-deps \
    "hive-data @ git+https://github.com/danielstewart77/hive-data.git@main"
pip install -r requirements.txt   # resolve anything this package now needs
```

Both halves matter. The version in the metadata does not change between
commits, so pip clones the repo, resolves `main` to a new commit, compares
that unchanged version against what is installed and installs nothing —
`--upgrade` included, since the comparison is the version and not the commit.
And `--no-deps` means the day this package takes on a new dependency, a
service that ran only the first command is running code whose driver is
absent. In Docker the requirements layer is cached on the contents of
`requirements.txt`, so a rebuild reuses the venv holding whatever commit was
current when that file last changed: an `ADD` of the commit object busts that
cache, and the force-reinstall has to run *before* `requirements.txt` is
resolved.

## Use

```python
import hive_data
from hive_data import core

await hive_data.connect()          # once, at startup
session = await core.get_session(session_id)
await hive_data.close()            # once, at shutdown
```

`HIVE_DATABASE_URL` names the database. Nothing else is required; the pool
size and every deadline have defaults and are configuration when they need to
be (`HIVE_DATABASE_POOL_MAX`, `HIVE_DATABASE_STATEMENT_TIMEOUT`,
`HIVE_DATABASE_ACQUIRE_TIMEOUT`, `HIVE_DATABASE_TRANSACTION_TIMEOUT`).

### Async only

There is no synchronous face and no threadpool bridge to write. Two faces
would mean every query existing twice forever, and the sync one silently
blocking somebody's event loop the first time a service forgot the bridge.
Nothing in the hive needs a sync one: every consumer is already async at the
top and reaches a sync `sqlite3` module through `run_in_threadpool` precisely
*because* the database layer is sync. That bridge comes out as each service
converts. Work that is genuinely CPU-bound — lucent's similarity scan over
vectors — belongs in `asyncio.to_thread` whatever the database is.

### Opening is explicit

Nothing opens a pool on demand. A lazy open under two concurrent first calls
builds two pools and holds double the connections against a ceiling shared
with every other service, with nothing reporting that it happened — so the
first operation against an unopened package raises `NotInitialised` and says
what was not done.

`connect()` also checks the applied schema against the columns these
operations name, so a database that is not the one they were written against
is a process that refuses to start. A column this package names and the
database does not have would otherwise arrive as a missing dict key, and a
missing key reads as a value: `session.get("rotation_armed") or 0` is
"nothing armed" on the one column whose three states decide whether a
permanent session row is kept or retired.

## What an operation hands back

A row is a plain `dict`. Nothing a caller imports, nothing with methods,
nothing that stops being JSON when it crosses an HTTP boundary. A read that
matches nothing returns `None` — not an empty dict, which would be a row with
no columns — and a list read that matches nothing returns `[]`.

A **write reports how many rows it changed**, as `{"rows": n}`, with
`"row"` alongside it when the statement returned one. That is not
bookkeeping: it is the arbitration the hive already runs on. The contest
between two people sending a bot's claim phrase at the same moment is an
`UPDATE ... WHERE claimed_ref IS NULL` followed by a row-count test — that
statement *is* the arbitration — and the same shape decides whether a broker
message was newly inserted and whether a session still exists to report
context for. A write that could only answer with rows would answer `[]` to
every one of them, and `[]` reads as success: two confirmed owners for one
bot.

Columns a caller must ask for by name are withheld by default.
`core.sessions.carry_forward` is a composed system prompt and
`resend_content` a queued user message, and the listings that read sessions
answer to the service token every surface bot in the hive holds — so one
careless `SELECT *` publishes a conversation's context to every bot. Pass
`private=True` to get them.

## Failures are told apart

A refusal reported as the wrong refusal sends somebody to the wrong machine.
`Unreachable` and `CredentialRefused` are two different hosts to go and look
at. `DatabaseMissing` means the server answered. `TimedOut` and
`NoConnectionAvailable` are neither of those — the database is up and serving
everybody else, and in the second case nothing was even sent, so nothing is
half-done and the remedy is this process's pool size. `Conflict` is separate
from `MissingReference` because the hive depends on the difference:
`add_message` is idempotent on purpose, since the SMS gateway re-POSTs the
same webhook on any non-2xx, so a duplicate reported as a generic rejected
write becomes a 500 and is retried forever.

Anything this package does not recognise is re-raised unchanged. A `NameError`
dressed as a `HiveDataError` is a bug wearing the costume of an outcome the
caller has a branch for, and the branch will swallow it.

## Units of work

One operation is atomic on its own. For a sequence, take a unit:

```python
async with hive_data.transaction() as unit:
    await unit.hold(f"session:{session_id}")
    await core.unbind_surface("terminal", "tile-1", unit=unit)
    await core.bind_surface(client_type="terminal", client_ref="tile-1",
                            session_id=session_id, unit=unit)
```

`hold` is what replaces the in-process `asyncio.Lock`es the gateway holds
today. Those are per process, and the only reason they sufficed is that comms
is the sole writer holding SQLite's read-write lock for the life of its
container — the exact property Postgres removes. The adopt sequence above is
a delete followed by a write; run twice concurrently without the lock it
leaves two active rows for one session, and the primary key catches nothing.

A unit refuses two things. It will not be **opened inside an async
generator**: `send_message` is a generator that writes to the database inside
its own loop over the model's response, so a unit opened there pins one
pooled connection for the length of a turn, and a closed browser tab abandons
the generator with the transaction still open, to be unwound whenever the
event loop's asyncgen finalizer reaches it. And it will not be **handed to a
second task**, because a transaction is one connection and sharing it means
each task can roll back the other's committed-looking work.

## Unpassed is not null

Optional parameters default to `UNSET`, and `None` means write null.
Collapsing the two can only express one of the two things the hive needs:
skipping every `None` never clears `harness_sid` — which has to be cleared so
a failed turn cannot resume a broken thread — and writing every `None` blanks
the context window to zero, which the surrounding code draws as a
conversation over its threshold.

## Column names are never a caller's

Values travel as parameters. Column names cannot: there is no placeholder for
an identifier. The gateway's dominant write assembles its `SET` clause at
runtime, so `update_session(**fields)` is the shape actually needed — and
every settable column is declared, with anything else refused as
`UnsettableColumn` rather than quoted and hoped for. An insert naming a
generated identity is refused too: Postgres accepts a value on a
`GENERATED BY DEFAULT` column without advancing the sequence, so the row
lands and the next insert collides. Round-tripping a row dict straight back
into an insert is how that happens by accident.

## Ordering

A ledger read orders by its identity column as well as by time, and the
identity is what breaks the tie. `session_turns.created_at` is whole epoch
seconds and `now()` is constant for a transaction, so two turns written in
one unit of work carry the same timestamp. An order without the tiebreak
returns them in whichever order the plan produced, and the visible failure is
the mind appearing to answer before it was asked.

## Scope

This package covers the `core` schema — the gateway's whole surface. `memory`,
`board` and `tools` follow as their services convert, one story each.

Two things are deliberately outside it. **hive-health** is not part of the one
database at all. The **inference proxy** keeps SQLAlchemy and its own store:
all six of its foreign keys are internal to its own schema, nothing else in
the hive reads its tables (the gateway and the minds get models over HTTP from
`/v1/models`), and it is a separately deployed product being replaced by a
more mature one. It is also the only service using an ORM, which is why this
package needs no ORM face.

## Tests

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The suite creates a database of its own per run and drops it afterwards — never
the live `hive` database, whose rows are the pre-cutover copies the loader
story still has to verify row for row. It applies
`hive_data/schema/core.sql`, a captured `pg_dump -s` of the applied `core`
schema vendored into the package: the generator that produced it reads the
seven live SQLite stores, so it cannot run from here, and a suite depending on
another checkout would fail on every other machine.

`HIVE_DATA_TEST_ADMIN_DSN` points it at a server. The default is the hive's
Postgres on its container network rather than loopback, and that is
load-bearing for one test: `pg_hba.conf` trusts loopback and demands scram
from everywhere else, so a wrong password over loopback *succeeds* and the
refused-credential test would pass for the wrong reason.
