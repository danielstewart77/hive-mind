# terminal

Standalone browser terminal for the hive mind, served at
terminal.sparktobloom.com. A single page (`/terminal`) listing live mind
sessions, with an xterm.js tile that attaches to a session's real pty over
`hive-comms`.

Auth is not its own — `auth.py` is a verbatim copy of the hive site's,
sharing the same sqlite db (`HIVE_DB_PATH`, mounted from the hive repo's
`data/` directory) and signing secret (`HIVE_SECRET_KEY`), scoped to the
`sparktobloom.com` cookie domain. A session issued by either site verifies
on both — logging into hive logs you into terminal, and vice versa.

## Run

```
docker compose up -d --build
```

Runs as container `terminal-site` on :8711, on the `hivemind` docker
network (to reach `hive-comms` and the voice server). The hive site's
`data/` directory is mounted directly, not copied, so both containers
read and write the same `hive.db`. `src/` is bind-mounted, so template
and static edits deploy live; route changes need a container restart.

`.env` keys: `HIVE_SECRET_KEY` (must match the hive site's),
`COMMS_BEARER_TOKEN`.

## Test

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
