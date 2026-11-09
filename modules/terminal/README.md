# terminal

Standalone browser terminal for the hive mind, served at
terminal.sparktobloom.com. A single page (`/terminal`) listing live mind
sessions, with an xterm.js tile that attaches to a session's real pty over
`hive-comms`.

The grid is painted by xterm's WebGL renderer: one canvas on one
composited layer, rather than the default renderer's few thousand row divs
and cell spans for a dropped invalidation to leave garbage across. Every
scroll also ends in a repaint, coalesced onto a frame so a screen's worth
of scrolling costs one repaint rather than one per line. If WebGL is
unavailable or the context is lost, xterm falls back to its DOM renderer
and a two-second sweep compares each painted row against the buffer it
came from, repainting any that disagree. `window.rendererInfo()` says
which renderer a tile is on; `window.screenText()` reads a tile's visible
rows out of the buffer, which is the only way to read a canvas-painted
tile without selecting it.

Auth is not its own, and no longer a copy of the console's either. `auth`
(the account store), `store` (the database under it) and `route_access` (the
route table's enforcement) are the console's own modules, installed from the
`hive_site` package in the hive repository as a second build context — the
same way `hive_data` arrives. `src/auth.py`, `src/store.py` and
`src/route_access.py` are three-line aliases binding them under the bare
name. They were verbatim copies for a year and the copies drifted: this one
still opened `hive.db` by hand and minted its own signing key into it while
the console moved onto a store named by `HIVE_DATABASE_URL`, so a password
change made in the console moved `session_version` in one store while this
site read the old value out of the other — which from a chair is being
signed out of the terminal and nowhere else.

The accounts live in the hive's Postgres at `HIVE_DATABASE_URL`, which is
required: unset, the site refuses to start. A session issued by either site
verifies on both, scoped to the `sparktobloom.com` cookie domain. Accounts are managed on the console;
the terminal has no user pages of its own beyond changing your password, and
a user holding a temporary password reaches nothing else until they do.

Who may reach which route is `src/terminal_access.py`, one table enforced in
front of every route and websocket: public, any signed-in user (the mind and
session listings filtered to the user's own minds), a mind owner's (every
session-keyed route and the attach websocket, resolved session to mind to
owner through comms), or admin-only. An admin reaches every session. A route
the table does not name is admin-only.

The label routes also take the mind's label token, which the middleware
verifies before the route runs; a wrong one is refused rather than answered
from the cookie, and a request with two Authorization headers is refused.
The attach websocket and the session event stream are decided again every
30 seconds while open, and closed once the user is disabled, reset or no
longer owns the mind. A refused attach is accepted and then closed with its
reason (4401, 4403, 4428) so the tile can read it and stop reattaching. Text
to speech speaks only in the voices of the caller's own minds; an admin's in
any.

## Run

```
docker compose up -d --build
```

Runs as container `terminal-site` on :8711, on the `hivemind` docker
network (to reach `hive-comms`, the voice server and the hive's Postgres).
`src/` is bind-mounted; the shared `hive_site` package is installed into the image,
so a change to it reaches this site by a rebuild; route changes need a
container restart.

`.env` keys: `COMMS_BEARER_TOKEN`; `HIVE_DATA_DIR` and, if the hive checkout
is not at the default path, `HIVE_SITE_DIR` — the two build contexts;
`HIVE_DATABASE_URL` (required); `HIVE_SECRET_KEY` only if
the console sets one (left unset on both, the key is generated once and kept
in the shared store); and, when the console signs in through auth-service,
the same `AUTH_SERVICE_URL` and `AUTH_SERVICE_ADMIN_TOKEN` (or
`HIVE_AUTH_BACKEND`) the console holds. The two sites must use the same
store to share a session.

## Test

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
HIVE_DATA_TEST_ADMIN_DSN=postgresql://… .venv/bin/python -m pytest
```

The suite runs on a scratch Postgres it creates, claims and drops on the
server `HIVE_DATA_TEST_ADMIN_DSN` names (see `hive_site/testing.py`), never
on the live database.
