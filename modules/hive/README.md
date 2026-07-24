# hive

Admin console for the hive mind, served at hive.sparktobloom.com. Hosts
the knowledge-graph explorer (`/graph`), the lucent memory browser
(`/memory`), and the standing-rules dashboard (`/rules`).

Auth lives in the hive itself — one sqlite db (`data/hive.db`) and a
signing secret, no external auth service — so a cloned hive is
self-contained. The terminal site shares this same db, secret, and
cookie domain, so one login covers both.

## Run

```
docker compose up -d --build
```

Runs as container `hive-site` on :8710, on the `hivemind` docker network
(for hive-lucent and hive-comms), with the lucent sqlite mounted
read-only for the memory browser. `src/` is bind-mounted, so template
and static edits deploy live; route changes need a container restart.

`.env` keys: `HIVE_SECRET_KEY` (session signing — share with the
terminal site), `LUCENT_BEARER_TOKEN`, `COMMS_BEARER_TOKEN`. On first
launch against an empty db, `HIVE_SEED_USERNAME` / `HIVE_SEED_PASSWORD`
create the one account; inert afterward.

## Test

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
