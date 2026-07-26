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

`.env` keys: `HIVE_SECRET_KEY` (session signing — share with the terminal
site), `LUCENT_BEARER_TOKEN`, and `COMMS_BEARER_TOKEN`. Capability Node
endpoints can be supplied with `OLLAMA_URL` and `PIHOLE_URL`; Hive Tools and
the voice servers use their existing service URLs. On first launch against
an empty db, `HIVE_SEED_USERNAME` and `HIVE_SEED_PASSWORD` create the one
account; inert afterward.

## Test

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
