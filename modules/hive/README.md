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
`COMMS_BEARER_TOKEN` authorize the core services;
`COMMS_ADMIN_BEARER_TOKEN` is required separately for every consequential
write — registering a Mind, changing its registry row, and setting its
default model. `HIVE_TOOLS_TOKEN` enables approval-gated Mind restarts from
the configuration page. Capability Node
endpoints can be supplied with `OLLAMA_URL` and `PIHOLE_URL`, then changed
without a restart from the Capability Nodes page. `EDGE_COMMS_URL`,
`EDGE_LUCENT_URL`, and `EDGE_HIVE_TOOLS_URL` are the LAN-reachable addresses
stamped into generated Edge Mind plans. Use
`EDGE_MIND_NAMES` for comma-separated remote minds that cannot be identified
from local config or an IP endpoint, and `EDGE_MIND_PROFILES` for profile
overrides such as `hex:sentinel`. On first launch against an empty db,
`HIVE_SEED_USERNAME` and `HIVE_SEED_PASSWORD` create the one account; inert
afterward.

The Inference Proxy Capability Node can clone its fixed public repository from
the control center. `INFERENCE_PROXY_DIR` selects the host destination mounted
for that action, and `INFERENCE_PROXY_URL` supplies a deployed endpoint when it
is not reachable as `http://inference-proxy:8888` on the Hive network.

## Configuring a Mind

A Mind's `runtime.yaml` is the durable truth about what it is, and each Mind
re-registers from that file every time it starts. So the control center
writes the file, not just the registry row: the console's
`PATCH /api/minds/<name>/configuration` calls the Mind's own
`PATCH /runtime`, then updates `broker.minds`. If the registry write fails, the file is put back.

That is one code path for every Mind — a container in this stack, a
bare-metal Mind on this host, and a Mind on another machine, which no bind
mount can reach. A Mind that is down cannot be configured, which is the
honest answer rather than a limitation: a registry row written while its
Mind was unreachable would be overwritten the moment it came back.

A new default model is live for the next session that Mind is asked for.
Conversations already running keep the model they started on, including
across rotations.

## Test

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
