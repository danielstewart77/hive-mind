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

## Dashboard

`/dashboard` is the live view: every conversation producing output right now
in its own column, each with how full its context is, above the machines the
minds run on and how much of each provider's subscription allowance is spent.
Below the columns, every other open conversation is a row with the size,
rotation point and window its mind last reported, read from the gateway's
table, so a conversation's room is visible before anyone types into it. A
size the harness estimated rather than the provider counted is marked so.

Four things are decided server-side rather than in the browser, so they can
be tested. **Which conversations hold columns** — four fit, the rest are
named as waiting, ordered by start time so the set does not reshuffle a
conversation out of view mid-sentence. **One machine, one row** — reports
merge on the host id each Mind returns, and a reading from outside a
container beats one from inside it, whose root filesystem is the image
layer. **Four failure states** — reporting, refused (401/503), unsupported
(404, a Mind without the route) and unreachable, because they are four
different remedies applied to four different machines. **One shared
snapshot** — every tab reads the same reading, or ten open tabs become ten
`nvidia-smi` invocations per interval on somebody's laptop; failures hold
the refresh window open too, since an erroring upstream polled by every tab
is exactly when hammering is worst.

Nothing unknown is drawn as zero. An absent token count, a provider that has
taken no traffic and a host that never answered each render as themselves —
a bar at zero for a conversation actually at 138k reads as room to spare. A
conversation genuinely at zero still reports zero: that is a measurement,
and only absence is unknown. A count past its rotation point is reported
past it rather than clamped, because a rotation fires on the next typed
message and that can be hours.

The page says when it cannot see. A gateway that is down returns a perfectly
good empty answer, which would otherwise render as a quiet hive; it is named
instead, and idle Minds are listed so "nobody is talking" and "nothing is
connected" stay distinguishable.

Both `/dashboard` and the `/api/dashboard` JSON behind it are login-guarded:
the JSON carries context counts, a machine inventory and subscription spend.
`INFERENCE_PROXY_KEY` is the console's own proxy client key, used to read the
model catalog (for each model's context window) and the provider allowance.
Without it the page still renders and those two panels report themselves as
not configured.

## Design sessions

`/design` holds the three pages a Mind fills in before writing code:
requirements, tests, and the call graph of what it intends to change. A
design session outlives the conversation that created it — the conversation
is a pointer on the record, not its owner — so a rotation leaves the work
intact and the next claim re-points it.

Each page carries two buttons. **Submit feedback** stores Daniel's edited
text and his per-item questions, then sends them into the bound
conversation. **Accept** advances; on page three it is **Build**, the go
signal.

The design page is a *surface*, like Telegram or the browser terminal. A
submission adopts the conversation through `activate_session` and arrives as
an ordinary user turn — nothing types into a pane. A keystroke injected into
a pty is indistinguishable from Daniel typing, which fires the pane's
rotation hook and respawns the pane out from under the delivery it was
carrying.

Three rules are enforced in the store rather than left to a Mind's manners:

- **Items are addressed by a stable id, never their number.** Inserting a
  requirement renumbers nothing, so feedback cannot reattach itself to a
  different item.
- **A revision may only change what Daniel touched, and only on page one.**
  An attempt to rewrite anything else is refused by name. It scopes to page
  one because his edit is what marks the later pages stale, and those rows
  carry no feedback of their own — enforcing it there would freeze a stale
  page with no way back.
- **Every write carries the revision it was read at.** A stale one is a 409.
  A revision that is optional on the wire is not a check at all.

Page two may not propose a test against a function nobody has looked at. An
existing function is resolved off real disk by the Mind that owns it
(`GET /symbols`, admin-guarded, same shape as `/skills` and `/files`) and the
row carries its real source, path, line span and a fingerprint. A function
that does not exist *yet* is marked planned and must name a row on page
three — which is why page three is published before page two even though
Daniel reads them the other way round. Anything else leaves the page
unresolved, and an unresolved page cannot be accepted.

`DESIGN_PUSH_TOKEN` is the credential a Mind publishes with. It has no
fallback to `COMMS_BEARER_TOKEN` or `COMMS_ADMIN_BEARER_TOKEN` on purpose:
every Mind in the hive already holds those, so either as a fallback would
let any Mind rewrite Daniel's requirements and press Build. Unset means the
design routes refuse with 503 rather than opening. Daniel's own browser
login is accepted on the same routes, reads included — a design session
carries source text pulled off his disks.

## Test

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
