# hive

Control center for the hive mind, served at hive.sparktobloom.com. It manages
Core Minds, Edge Minds, Capability Nodes, sessions, setup, knowledge, memory,
and standing rules. Edge Mind deployments use the
[`hive-edge-minds`](https://github.com/danielstewart77/hive-edge-minds)
runtime.

Auth lives in the hive itself — accounts and a signing secret, nothing external — so a cloned hive is self-contained. The terminal site shares
the same store, secret and cookie domain, so one login covers both.

## Where the data is

Nothing in `src/` opens a database or writes SQL. Every page and every route
calls a named operation in `store.py`, answered by the hive's one Postgres
through [`hive-data`](https://github.com/danielstewart77/hive-data) at
`HIVE_DATABASE_URL`. There is no other store: with `HIVE_DATABASE_URL` unset
the console refuses to start. The SQLite file it used before the cutover is
retired and nothing reads it.

The composite rules are not in `store.py`. Which parents an item may be
given, what a board drop does to two sibling lists, when a design page may
advance, whether a change would leave the hive with no enabled admin — those
live in `kanban_data`, `design_data` and `auth`, once, on top of the store's
operations. The store is async, which is why the console is async all the way
down to `require_auth`.

The memory pages read the `memory` schema of the same database.
The board and the memory pages answer to a signed-in session; the board also
answers to a mind's own key (below).

The suite runs on a scratch Postgres: set `HIVE_DATA_TEST_ADMIN_DSN` to a
server it may create and drop a database on (see `hive_site/testing.py`).
The browser tests drive the real pages in headless Chromium:
`.venv/bin/python -m playwright install chromium` once.

## Run

```
docker compose up -d --build
```

Runs as container `hive-site` on :8710, on the `hivemind` docker network
(for hive-lucent, hive-comms and the hive's Postgres). `src/` is
bind-mounted, so template
and static edits deploy live; route changes need a container restart.

Copy `.env.example` to `.env`. This site holds no key for the gateway or for
the tools: each call it makes is made for a signed-in person and carries that
person's own key (an approval-gated Mind restart, a rename, a registry read).
`LUCENT_BEARER_TOKEN` is the memory store value the rules page still uses.
`HIVE_SITE_PUBLIC_URL` is the address of this site as a browser reaches it,
which hive-tools puts in an approval notice. Capability Node
endpoints can be supplied with `OLLAMA_URL` and `PIHOLE_URL`, then changed
without a restart from the Capability Nodes page. `EDGE_COMMS_URL`,
`EDGE_LUCENT_URL`, and `EDGE_HIVE_TOOLS_URL` are the LAN-reachable addresses
stamped into generated Edge Mind plans. Use
`EDGE_MIND_NAMES` for comma-separated remote minds that cannot be identified
from local config or an IP endpoint, and `EDGE_MIND_PROFILES` for profile
overrides such as `hex:sentinel`.

## Creating a container Mind

The Add a Mind form defaults to DSH from
[`hive-harness-dsh`](https://github.com/danielstewart77/hive-harness-dsh).
Claude CLI and Codex CLI remain selectable. DSH uses the inference proxy
for OpenAI, Anthropic, and Ollama models, with the new Mind's own proxy key.

The supplied key may already be in use. Hive accepts a key the proxy accepts
and shows a warning when it matches an existing local proxy credential.
It uses that key for the new Mind without changing other Minds' keys.

An existing local Mind's detail page also accepts a pasted proxy key. Saving
checks and stores the key without returning it. A Mind reads its key from a
file (`/run/mind-key/key`) in a folder only that Mind has mounted, read-only,
outside the checkout (`HIVE_KEY_ROOT` in the console, `HOST_KEY_DIR` in a new
Mind's compose); the console writes it atomically, and the Mind takes the new
key at its next request with no restart. A Mind made before key files is
recreated once to add the mount.

Build the DSH tree through the Hive installer before creating a DSH Mind.
Its generated container mounts that tree read-only at `/opt/dsh` from
`HOST_DSH_DIR`, defaulting to `${HOST_PROJECT_DIR}/dsh`. Each Mind has its
own writable `.dsh` home. The container generates its Hive profile and hooks
before starting the Mind server, and uses an executable `/tmp` for DSH's
native addon.

## Accounts and who sees what

The Chief of Staff chart uses the Mind assigned the `chief-of-staff` role.
With no assigned Mind, its root has no name or portrait. Assign or clear the
role from the Mind's configuration page; the chart reads that same assignment.

Accounts live in one of two stores, behind one interface (`auth`), and an
install uses exactly one. That interface, the data store under it and the
authorization middleware in front of it are the `hive_site` package in this
repository, installed into this image and into the terminal site's from the
same directory — one copy, not two. `src/auth.py`, `src/store.py` and
`src/route_access.py` are three-line aliases that bind it under the bare
name, so `import auth` here is the module the terminal runs. Both sites read
their accounts from the hive's Postgres at `HIVE_DATABASE_URL`. With no other
configuration the console signs in against those built-in accounts: on first
launch against an empty store,
`HIVE_SEED_USERNAME` and `HIVE_SEED_PASSWORD` create the first admin (inert
afterward), and `HIVE_SECRET_KEY` signs only the short note a Microsoft sign-in in
progress keeps in the browser (left unset, a random one is generated once and
kept in the store, which the terminal site shares).

Signing in makes a fresh random key for that sign-in. It goes to the browser
as the `hive_session` cookie (HttpOnly) and only its one-way scramble is
written, to the hive's record of keys, with an end twelve hours later. Using
the site does not lengthen it and there is no idle limit; signing out ends
that device's key, `POST /api/sign-ins/end` ends all of your own, and an
administrator can end anyone's (`POST /api/admin/users/{username}/sign-ins/end`).
Turning an account off, changing a password or resetting one ends all of that
person's. No other program is involved; the hive does not use the separate
authentication service, and a site whose settings still name it
(`AUTH_SERVICE_URL`, `AUTH_SERVICE_SITE`, `HIVE_AUTH_BACKEND`) refuses to
start and says to remove them.

The session cookie is marked Secure only when the browser came over https —
the request's own scheme, or `X-Forwarded-Proto` from a TLS-terminating
proxy — so sign-in works over plain http at an IP address. It is host-only
unless `HIVE_COOKIE_DOMAIN` names a parent domain shared with the terminal.
`HIVE_TERMINAL_URL` is the terminal's address for the console's links to it;
unset, those links are not drawn.

Admin is membership of the `admins` group, managed under Settings, Users
and groups. A new user signs in with the temporary password an admin set and
must choose their own before any other page opens; a reset does the same
again. Users are disabled, never deleted, and the last enabled admin can be
neither disabled nor removed from `admins`.

Microsoft (Entra ID) and Windows domain (Kerberos) sign-in are switched on by
configuration alone and sign in only to accounts an admin created; what IT
has to provide and every setting is in
[docs/sign-in-microsoft-windows.md](docs/sign-in-microsoft-windows.md).

Every route is classified in one table, `src/console_access.py`: public, a
signed-in user's (listings filtered to their own minds), a mind owner's
(resolved from the route's mind name, mind id, session id or operation id
through comms' `owner` on `GET /broker/minds`), or admin-only. A route the
table does not name is admin-only, websockets included; the enforcement is
`src/route_access.py`, in front of every route.

A route a mind reaches with a bearer (the canvas snapshot and push, the
design routes) names that bearer in the table, and the middleware verifies
it before the route runs; a wrong one is refused, never answered from a
cookie beside it, and a request carrying two Authorization headers is
refused outright. A connection that stays open — `/ws/canvas` — is decided
again every 30 seconds and closed once its user may no longer hold it. A
refused websocket is accepted and then closed with its reason (4401 signed
out, 4403 not allowed, 4428 password to change), so a browser can read it.

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
mount can reach. The console presents the signed-in
owner's own key (made by the console, kept like a mind's, shown to nobody); the
Mind accepts that key, or its own, and asks the gateway whether a key that is
not its own is its owner's. So a Mind on another machine is changed without the
console holding that Mind's key; a Mind that cannot reach the gateway answers
503. A Mind that is down cannot be configured, which is the
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
The rotation point is a property of the model: the proxy's row for it carries
`rotation_threshold_percent` beside `context_window`, the page shows that
percentage beside the threshold, and a conversation whose mind reported no
threshold is drawn at that percentage of its window. A Mind's model picker
shows each model's percentage too.

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
model catalog (for each model's context window and rotation threshold
percentage) and the provider allowance.
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

A mind draws on the canvas with its own key (`MIND_KEY`), and publishes
design pages with it only while it holds the `operator` role; recognised from the one record of keys in the hive database. A
person's key presented by a mind is refused, and a key cut off or replaced
stops working on the next request. There is no design or canvas token.
Daniel's own browser login is accepted on the same routes, reads included — a
design session carries source text pulled off his disks.

A mind reads and writes the Kanban board, and changes roles, with its own key
(`MIND_KEY`, the one key it uses for everything). The console finds whose key
it is in the one record of keys in the hive database and checks the role the
mind holds, read on every request: `board-planner` reads, creates and updates
(every board route but the delete), `board-push` does all of that and deletes
(`DELETE /api/kanban/{kind}/{record_id}`), and `operator` reaches the routes a
person uses on the Roles page. A mind with none of these is refused, a person's
key presented by a mind is refused, and a key cut off or replaced stops working
on the next request. There is no board, roles or site administrator token: none
is read anywhere. The compose fragment of a mind holding the operator role gets
the skills folder writable and `HIVE_SITE_URL`, nothing else
(`src/operator_attachment.py`).

## Test

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Inference providers

Settings → Providers stores named endpoint URLs and API formats for direct OpenAI, Anthropic, Ollama, and proxy/custom connections. Tokens belong to Minds; enter a token when creating a Mind or on its configuration page. The new-Mind form lists models from the selected endpoint using the entered Mind token. Connections that allow unauthenticated requests can use a blank token. Reused tokens are accepted with a warning.

Existing local Mind connections are imported without rewriting their runtime files, model choices, or credentials. Provider URL edits take effect for an existing Mind when it is explicitly reloaded. A provider assigned to Minds cannot be deleted or have its API format changed.

New Minds need the configured-provider hive-mind runtime. During staged deployment, HIVE_MIND_BUILD_CONTEXT selects that source; HIVE_NEW_MIND_DSH_DIR selects the host DSH tree mounted by newly generated Minds, leaving existing fragments unchanged.
