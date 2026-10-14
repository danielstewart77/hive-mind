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

Auth is not its own. `src/auth.py` (the account store) and
`src/route_access.py` (the route table's enforcement) are verbatim copies of
the hive console's, so the terminal signs in against the same store the
console does — hive.db (`HIVE_DB_PATH`, the console's own db, mounted from
the hive repo's `data/` directory) or auth-service, chosen by the same
variables — and a session issued by either site verifies on both, scoped to
the `sparktobloom.com` cookie domain. Accounts are managed on the console;
the terminal has no user pages of its own beyond changing your password, and
a user holding a temporary password reaches nothing else until they do.

Who may reach which route is `src/terminal_access.py`, one table enforced in
front of every route and websocket: public, any signed-in user (the mind and
session listings filtered to the user's own minds), a mind owner's (every
session-keyed route and the attach websocket, resolved session to mind to
owner through comms), or admin-only. An admin reaches every session. A route
the table does not name is admin-only.

## Run

```
docker compose up -d --build
```

Runs as container `terminal-site` on :8711, on the `hivemind` docker
network (to reach `hive-comms` and the voice server). The hive site's
`data/` directory is mounted directly, not copied, so both containers
read and write the same `hive.db`. `src/` is bind-mounted, so template
and static edits deploy live; route changes need a container restart.

`.env` keys: `COMMS_BEARER_TOKEN`; `HIVE_SECRET_KEY` only if the console sets
one (left unset on both, the key is generated once and kept in the shared
db); and, when the console signs in through auth-service, the same
`AUTH_SERVICE_URL` and `AUTH_SERVICE_ADMIN_TOKEN` (or `HIVE_AUTH_BACKEND`)
the console holds. The two sites must use the same store.

## Test

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
