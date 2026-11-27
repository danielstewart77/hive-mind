# hive-tools

The **body** of a [hive-mind](https://github.com/danielstewart77/hive-mind)
deployment: one FastAPI service that gives AI minds hands in the outside world.
Where the mind reasons, hive-tools acts — reading mail, booking calendar events,
driving a browser, sending SMS, managing containers, and running fine-tunes.

Every capability is a router under `tools/`, mounted only if its dependencies
import cleanly, so a missing Google credential disables the Gmail routes rather
than taking the service down.

## Capabilities

| Router | What it does |
|---|---|
| `gmail` | Read, search, and send mail via the Gmail API |
| `calendar` | Read and create Google Calendar events |
| `browser` | Fetch and extract page content |
| `linkedin` | Post and read via the LinkedIn API |
| `docker_ops` | Inspect and manage containers on the host daemon |
| `ollama` | Chat, structured output, and model listing through the hive's inference proxy (the paths keep their old name) |
| `sms` | Send SMS through a self-hosted android-sms-gateway |
| `training` | Curate captured turns and launch LoRA fine-tune runs |
| `schema` | Serve the tool schemas minds use for discovery |

## Human-in-the-loop

Outward-facing actions are gated. `hitl_gate("<tool_name>")` holds the mind's
call **open** until a person decides it. The mind calls a gated tool such as
send mail and simply waits on that call, as it would on any slow tool. The
tools record the request (who asked, for what, when) and notify the operator
with a link; the owner of the mind, or an administrator, approves or denies it
on the hive site. On approval the same open call is released, runs the tool,
and the tool's result is the reply to that call. On a denial, or when
`HITL_WAIT_SECONDS` runs out with nobody having decided, the same call is
answered with a plain refusal telling the mind no (403 for a denial, 408 for no
answer in time; the body has `approved: false` and an `error` sentence). Nothing
is replayed later, no mind's key is stored or used again, and no message is sent
to the mind afterwards: there is nothing to tell it that its own call has not
already said.

New tool names auto-register into `tool_hitl_settings` at startup
(`sync_tool_registry` in `hitl.py`) defaulting to **always prompt** — a newly
added tool is gated by default, never open by default. That seed is
insert-if-absent: it runs for every gated tool on every start, so a replacing
write would re-arm every gate the operator had turned off on each deploy.

`HITL_WAIT_SECONDS` (default 300, five minutes; zero refuses at once; a value
that is not a whole number falls back to 300) is how long a call is held. Five
minutes is long enough for a person to be paged, open the hive site and
decide, and short of the limits a caller commonly has: a shell tool call is
two minutes by default and ten at most (`BASH_DEFAULT_TIMEOUT_MS`,
`BASH_MAX_TIMEOUT_MS`), the hive site waits 310 seconds on a gated call, and
the gateway's 45 second forward does not carry tool calls. A mind whose own
tool-call limit is shorter than the wait should be given a smaller wait.

A request is decided on the hive site, by a person signed in as themselves:
the hive site sends that person's own key to `POST /hitl/{id}/respond`, and the
tools accept it only from the owner of the mind that asked or an administrator,
recording that person's name on the request. A mind's key is refused with a 403
before anything is decided, so the request stays pending and the call stays
held. `GET /hitl/{id}/review` (the whole call, for a person who may decide it)
and `GET /hitl/waiting` (what waits for the caller) take a person's key too.
`GET /hitl/{id}` is only the record: who asked, which tool, when, its status and
who decided it. The notice carries `HIVE_SITE_PUBLIC_URL/approvals/{id}`; there
is no button and no approver credential.

A decision is one guarded statement, so two people reaching the queue at the
same moment produce one decision and the loser is told which of three
reasons it lost — no such request, already settled, or expired while the
page was open. Those have different remedies, which is why they are not
folded together. An expired request is drawn as expired: the expiry is a rule
applied when the row is read, and `effective_status` is the one place it
lives. A held call closes its own request when its time is up, so a yes that
arrives a moment later finds it closed and runs nothing.

The decision and the sudo grant it may earn are one unit of work. Committed
separately they leave an end state nothing can recover from — the request
says approved with no grant, and the decision is guarded on `pending` so it can
never be approved again.

A held call lives in the process that holds it. If the caller hangs up, its
request is closed at once; if the tools restart, every request still waiting is
closed at the next start (`close_waiting_requests`), because the call that was
waiting on it is gone and a yes now would run nothing.

A gated call whose body holds a NUL byte is refused with a 400 rather than
held. A NUL is valid UTF-8, so `errors="replace"` leaves it alone, and
Postgres refuses it outright. Refused rather than escaped, because the person
must see exactly the call they approve, and a body stored with the NUL taken
out is a different call.

## A mind's one key

A mind presents one key to the gateway, the memory store and these tools, and
each knows which mind is calling: the tools record a gated call under the
holder the key belongs to and name that mind in the approval notice. The console registers the key the person
pasted when the mind is created, with these admin-guarded routes (bearer
`TOOLS_ADMIN_BEARER_TOKEN`, held by the console and by no mind):

- `PUT /credentials/{holder}` with `{"token": "<value>"}` registers the value
  for that holder, replacing any it had: a holder has one working key. A value
  another holder already has, live or revoked, is refused naming that holder.
  The holders `hitl-approver`, `internal` and `skippy` are refused on every
  credential route, so no administrator credential can make an approver.
- `DELETE /credentials/{holder}` revokes the holder and expires its pending
  requests, so approving one later runs nothing. `?remove=true` deletes the
  holder's rows, freeing the value for a mind (the undo of a creation).
- `GET /credentials` lists holders, whether each is live, never a value or digest.

What a mind's key reaches is what the shared service token reached, less the
approver: the same routes, with docker routes still gated by approval and by
the owner check. It never decides an approval: `POST /hitl/{id}/respond` answers
403 to every mind and the request stays pending. A renamed mind keeps its
tools holder (the rename moves `tools.api_tokens` with the rest).

## Where the data lives

The `tools` schema of the hive's Postgres, through `hive_data`, at
`HIVE_DATABASE_URL`. It is required: unset, the service refuses to start —
before the first-boot admin seed below can read an empty store and seed it.
The SQLite file this service used before the cutover is retired and nothing
reads it. Nothing outside `store.py` opens a database or writes SQL.

## Running it

```bash
cp .env.example .env        # then fill in real values — see the warning below
docker compose up -d --build
```

The image takes `UID`, `GID` and `DOCKER_GID` build args (defaults 1000, 1000,
983) for the app user and the group that owns the host's Docker socket; pass
the host's own values (`--build-arg DOCKER_GID=$(getent group docker | cut -d: -f3)`)
anywhere else. No GPU is needed: only the training router's VRAM arbitration
calls `nvidia-smi`, and without it the service starts and serves, reporting no
card.

The service listens on `9421`. FastAPI's `/docs`, `/redoc`, and `/openapi.json`
are disabled deliberately: an exposed OpenAPI spec is a machine-readable map of
every route and schema, and this service's routes reach real mail and real
phones.

### Security notes

- This service has no pages or sign-in of its own (no `HT_SECRET_KEY`, no
  `HT_ADMIN_PASSWORD`): a person acts with their own key from the hive site.
- `credentials/` and `data/` are ignored wholesale, not by file extension —
  OAuth tokens and service-account JSON never enter git.
- The Docker socket is mounted for the `docker_ops` router. That is root-
  equivalent access to the host daemon. Run this service only on a host you
  trust it with, and keep `9421` off the public internet.

## Deploying from hive-mind

This folder is `modules/hive-tools` of hive-mind, a tracked directory with its
history. It is built and started on its own, from the one checkout, and
nothing else is rebuilt:

```bash
modules/hive-tools/deploy.sh          # build from the committed tree, recreate hive-tools
```

The image is built from an export of the committed tree (this folder and
`modules/hive-data`), never from working files that a mind with the checkout
mounted could have edited. Its `.env`, `credentials/` and `data/` live in
`~/.config/hive-tools` (`HIVE_TOOLS_STATE_DIR`), outside the checkout. The
folder must stay named `hive-tools`: `tools/docker_ops.py` refuses to compose
down a project whose folder has that name. This is the only hive service that
mounts the Docker socket.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
HIVE_DATA_TEST_ADMIN_DSN=postgresql://... .venv/bin/python -m pytest   # zero failures is the bar
```

The suite runs on a scratch Postgres it creates, claims and drops on the
server `HIVE_DATA_TEST_ADMIN_DSN` names, never on the live database.

Tests land with code changes.

## Adding a tool

1. Write `tools/<name>.py` exposing an `APIRouter`.
2. Mount it in `server.py` behind a guarded import, matching the existing
   pattern.
3. Gate any outward-facing action with `hitl_gate("<tool_name>")`.
4. Add tests under `tests/api/`.

## License

MIT — see [LICENSE](LICENSE).
