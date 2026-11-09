# hive-site

The accounts, sessions and data store that the hive console (`Dev/hive`) and
the terminal site (`Dev/terminal`) both run. One copy, installed into each
image from this directory as a build context — the same way both take
`hive_data`.

- `hive_site.store` — every operation either site performs on the hive's
  database, as one async surface over the hive's Postgres through
  `hive_data`, at `HIVE_DATABASE_URL`. Unset, a site refuses to start.
- `hive_site.auth` — accounts and sessions on top of it: the `builtin` store
  and the `auth-service` store, the session cookie, and the request
  resolution both sites' routes depend on.

## Why the whole store moved and not just its accounts half

The terminal needs twenty-two account operations and none of the board,
design or memory ones. Moving only those twenty-two would have meant a
second copy of the connection and unit-of-work plumbing underneath them, and
an account write issued inside a board transaction would then sit outside
that transaction. A unit of work has to be one connection, so the operations
that share one have to be one module.

So the terminal installs operations it never calls. That is the cost, and it
is the cheap half of the trade: the expensive half was two drifting copies
of `auth.py`, which is what this package ends.

## The site modules are aliases, not copies

Each site keeps `src/auth.py` and `src/store.py`, three lines each, binding
this package's module into `sys.modules` under the bare name. `import auth`
in either site therefore *is* this module — not a re-export of it — so a
test that patches `auth.get_store` patches the thing the routes call, and
neither site can drift from the other by editing a local file.
