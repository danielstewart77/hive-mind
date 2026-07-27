# hive

Control center for the hive mind, served at hive.sparktobloom.com. It manages
Core Minds, Edge Minds, Capability Nodes, sessions, setup, knowledge, memory,
and standing rules. Edge Mind deployments use the
[`hive-edge-minds`](https://github.com/danielstewart77/hive-edge-minds)
runtime.

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

Copy `.env.example` to `.env`. `HIVE_SECRET_KEY` signs sessions and must be
shared with the terminal site. `LUCENT_BEARER_TOKEN` and
`COMMS_BEARER_TOKEN` authorize the core services. `HIVE_TOOLS_TOKEN` enables
approval-gated Mind restarts from the configuration page. Capability Node
endpoints can be supplied with `OLLAMA_URL` and `PIHOLE_URL`, then changed
without a restart from the Capability Nodes page. `EDGE_COMMS_URL`,
`EDGE_LUCENT_URL`, and `EDGE_HIVE_TOOLS_URL` are the LAN-reachable addresses
stamped into generated Edge Mind plans. Use
`EDGE_MIND_NAMES` for comma-separated remote minds that cannot be identified
from local config or an IP endpoint, and `EDGE_MIND_PROFILES` for profile
overrides such as `hex:sentinel`. On first launch against an empty db,
`HIVE_SEED_USERNAME` and `HIVE_SEED_PASSWORD` create the one account; inert
afterward.

## Test

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
