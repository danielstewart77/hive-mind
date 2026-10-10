# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

**Hive Mind** is a self-improving personal assistant powered by Claude Code. The system uses a **centralized gateway server** that wraps the Claude CLI's bidirectional stream-json mode, giving every client (Discord, terminal, web) full CLI capabilities through one API.

The nervous system — lucent (vector store + knowledge graph) and comms (the gateway: session manager, broker, HITL) — lives in-repo under [`nervous-system/`](nervous-system/), running as the `hive-lucent` and `hive-comms` containers (compose services `lucent` and `comms`). Everything reaches both over HTTP+bearer; minds hold no lucent or gateway code. The standalone `hive_nervous_system` repo is retired — its code was folded in here.

### Architecture

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│ Discord Bot │  │ Telegram Bot│  │ Group Chat  │  │  Scheduler  │
│  (thin)     │  │  (thin)     │  │  Bot (thin) │  │  (cron)     │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │                 │
       └────────────────┼────────────────┴─────────────────┘
                        │  HTTP / WebSocket
                 ┌──────▼──────┐
                 │  hive-comms │
                 │   Gateway   │  ← nervous-system/comms/server.py
                 └──────┬──────┘
                        │
              ┌──────────▼──────────┐
              │   Session Manager   │  ← nervous-system/comms/sessions.py
              │   (process pool +   │
              │    SQLite DB)       │
              └──────────┬──────────┘
                         │  mind_id routing
         ┌───────────────┼───────────────┬──────────────┐
  ┌──────▼───────┐ ┌─────▼──────┐ ┌─────▼──────┐ ┌────▼─────────┐
  │ Ada          │ │   Bob      │ │   Bilby    │ │  Nagatha     │
  │ (CLI Claude) │ │(CLI Ollama)│ │ (SDK Code) │ │ (Codex CLI)  │
  └──────┬───────┘ └─────┬──────┘ └─────┬──────┘ └────┬─────────┘
         └───────────────┴───────────────┴──────────────┘
                         │  HTTP + bearer
          ┌──────────────┴──────────────┐
   ┌──────▼──────┐               ┌──────▼──────┐
   │ hive-lucent │               │ hive-tools  │
   │ vector + KG │               │ Gmail/Cal/  │
   │ (shared)    │               │ Docker/HITL │
   └─────────────┘               └─────────────┘
```

### Self-Improvement

When a user requests something no existing tool handles, Claude Code:
1. Generates the tool code by chaining available terminal tools
2. For requests that are frequent or could benefit from more structure, use the `/tool-creator` skill to create a new tool
3. If an API key is needed, asks the user and uses the `/secrets` skill to store it
4. The new tool is immediately available for use

### Backend Flexibility

A mind names a provider in its own `runtime.yaml`, and the model it runs by
that model's proxy deployment name. The inference proxy owns the mapping from
model to upstream, so adding a provider is a row there rather than a change
here, and one locally-hosted model can serve a claude harness and a codex
harness both.

### One mind server, every harness

A conversation runs on a harness — `claude`, `codex` or `dsh`, always the bare
name — and can be switched mid-conversation, so a container runs
`minds/mind_server.py` rather than one harness module. It mounts all three
adapters and routes each session by the harness its spawn payload names, a
terminal attach by its `harness` query, and a pane rotation by the `harness`
in its body. Each adapter keeps its own session table, thread map and process
handling; the server adds only a session-to-harness map. `runtime.yaml`'s
`harness` is the default for a *new* conversation, and `PATCH /runtime` sets it
only together with a `default_model` that harness's proxy listing offers, in
one write, or refuses and writes nothing.

`GET /harnesses` (admin-guarded) reports each harness as offered or not, with
the reason: its CLI on PATH (dsh: the launcher in the mounted tree), its hooks
configured (Stop carrying `auto_remember` and `rotation_check`, plus a
UserPromptSubmit hook — read from claude's `settings.json`, codex's
`config.toml`, or `DSH_HOOKS_CONFIG`), and a login (claude's credentials file
or token, codex's `auth.json` or key, dsh's proxy key). The checks are a list,
`mind_server.CHECKS`, so a deployment whose login lives elsewhere replaces one.
`GET /models?harness=` relays that harness's listing.

A switch hands the old conversation over as **history, not a session**:
`POST /handover` (admin-guarded) reads the outgoing harness's own transcript
through `minds/transcript.py` — one reader per harness, one renderer — and
returns plain text: the summary whole in front, prose and tool calls whole,
each tool result cut to its first lines and marked `[trimmed]`, oldest
transcript dropped first to fit the byte budget (half the window at four bytes
a token, never past 120,000). A conversation with no transcript on disk has
never had a turn and hands over the summary alone, or nothing; one whose
transcript exists and cannot be read hands over the summary alone, and with no
summary either the switch is refused with 422 and the old harness keeps
running. The
spawn that follows carries the text as `opening_turn`, which every adapter puts
in front of the first user message it sends (`handover\n\n---\n\nmessage`) —
stdin, stream-json or task file, never argv — so it lands in the new
transcript and survives the next switch. A pane takes a carry-forward the same
way, as its first user turn. `DELETE /sessions/{id}?forget_thread=1` also
drops codex's thread for the session; a plain kill keeps it, so a respawn
rejoins its thread.

Each process a conversation runs in is told its own model's window
(`HIVE_MODEL_CONTEXT_WINDOW`, and `HIVE_ROTATION_THRESHOLD_TOKENS` at this
mind's `rotation_threshold_percent`), looked up from the proxy for that
conversation's harness and model. `model_context_window` in the file is the
default model's, which is the wrong room for a conversation switched to
another; a rotation hook reads the environment first. A dsh conversation on a
mind with no `context_window` line is sized by the same window.

The image carries claude and codex; **dsh is mounted, not baked** — the
harness tree is a built working copy, bind-mounted read-only at `/opt/dsh`
with `DSH_BIN` naming its launcher, the pattern Cypher established. Baking it
would mean a monorepo build inside the image and a harness frozen at each
rebuild. A host without the tree gets dsh reported unavailable, not a mind
that fails to start. Codex and dsh keep their own homes (`CODEX_HOME`,
`DSH_HOME`, defaulting to `minds/<name>/.codex` and `.dsh`); a mind's
`runtime_config_dir` is read only by the harness it was written for.

### What a dsh mind needs that the other two do not

`minds/harness/dsh_cli.py` drives our fork of the DeepSeek harness
(`github.com/danielstewart77/dsh`), which is not an npm install: the tree is a
built working copy, bind-mounted into the mind's container, and `dsh_bin` in
`runtime.yaml` names the launcher inside it. Two container requirements follow
from that and are easy to get wrong, because each fails as something else.

**Node 22 or newer.** The harness imports `parseEnv` from `node:util`, which
Ubuntu 24.04's apt `nodejs` (18.19) does not export, so the launcher dies on
its first import before any turn exists. The image therefore installs Node
from NodeSource rather than from apt; claude and codex both run on 22 as well,
so one node serves every harness.

**`/tmp` mounted exec.** Docker's tmpfs default is `noexec`. dsh's plugin
loader reaches Node's internal ESM loader through a native addon, whose own
prebuild loader copies the `.node` into a cache under `/tmp` and dlopens it
from there — on a noexec mount that fails as "failed to map segment from
shared object". The loader then silently falls back to a bare `import(name)`
anchored at its own file rather than at the profile directory, and the mind's
own profile bundle is reported as a package that does not exist. The
container is the boundary for a mind, not the mount flag, so
`tmpfs: - /tmp:exec,mode=1777`.

**A non-interactive permission mode.** dsh's base bundle pins a fresh session
to `workspace-write` with an `ask` approval policy, and a per-turn harness has
nobody to ask — no pane, no prompt, no channel the request travels on. The
escalation is refused, and what reaches the operator is `bash` failing
`SANDBOX_UNAVAILABLE` and a write outside the spawn's own cwd failing
`FS_SANDBOX_DENIED`: capabilities the model appears to lack, when they are
walls the deployment put up. So every spawn carries
`DSH_PERMISSION_MODE=danger-full-access`, the same decision the claude and
codex minds already run under — the boundary is the container and the
directories mounted into it. A mind that wants confinement anyway names
`permission_mode` in its own `runtime.yaml`.

A mind's settings are a form on the console, not a file to open.
`minds/runtime_settings.py` declares the vocabulary — one entry per control,
with its label, kind, range and the comment written above a line it creates —
and `GET`/`PATCH /runtime/settings` relay it. Which controls appear is decided
by the **harness**: only dsh drives a dispatch as a goal and only dsh enforces
a turn timeout, so a Claude or Codex mind is never offered a box its runner
would ignore. A declared schema rather than the file's own keys, because the
file carries identity, plumbing and an `env` block holding this mind's proxy
key beside its settings — and because a setting the file has no line for yet
would otherwise be unreachable. Values are rendered per kind before anything
is written: `str(False)` is a string a YAML reader loads as truthy, and a
description holding a bare colon splits its line into a nested mapping.

`turn_timeout_seconds`, `goal_rounds` and `stop_on_failed_call` are re-read
from the file on **every turn** (`dsh_cli.live_runtime`), so a value the
console writes takes effect on the next turn rather than on the next container
start — otherwise the panel would be a form whose every edit waited on a
restart. A timeout of zero means no bound at all, which is why it is resolved
through `turn_timeout` rather than `float(raw or 1800)`: that expression read
zero as absent and handed back a tighter bound than the default it was trying
to escape.

`model_context_window` is reported beside the settings and is editable
nowhere. The window belongs to the model, the inference proxy is the only
thing that knows it, and `PATCH /runtime` writes it in the same write as the
model — because what needs it is a per-turn hook sizing a rotation threshold
from a percentage, and a hook that called the proxy would pay for it every
turn. A model the proxy has not measured writes zero rather than leaving the
previous model's figure to be multiplied by that percentage, and no request
body may name the field: a window taken off a request is a number nobody
measured.

It is deliberately **not** `context_window`. That key is dsh's own — the
serving ceiling its profile declares, required at the adapter's boot and read
by its compaction — and it is a different number from a different source.
Writing a model's nominal window over it is how a model save stops a mind
starting.

A write reads the document back before it replaces the file, and compares each
value against what was asked for. One-line substitution cannot express every
way YAML states a value, and the dangerous cases parse cleanly: a folded or
multi-line value keeps its continuation lines, which attach to the new scalar,
and a file carrying a key twice has its first occurrence replaced and its last
one read — a save reporting success over a change that never took. The
original's mode is carried across the replace, since a config going 0600
is a read from another account failing with no edit to explain it.

The profile itself lives under the mind's `DSH_HOME`
(`profiles/<dsh_profile>/`), with the hive surface package symlinked into its
`node_modules/@hive/`. dsh resolves every in-box bundle from its own
installation, so that one link is all a profile needs.

Per-subprocess env isolation — no global env mutation.

## Quick Start

```bash
docker compose up -d --build
```

## File Structure

```
hive-mind/
├── nervous-system/                # Lucent + comms (see nervous-system/README.md)
│   ├── lucent_api/               # Vector store + KG (hive-lucent container)
│   ├── comms/                    # Gateway: sessions, broker, bootstrap, HITL (hive-comms container)
│   ├── tests/                    # Comms test suite (lucent's is lucent_api/tests/)
│   └── data/                     # lucent.db, broker.db, sessions.db (gitignored)
├── config.py                      # Centralized config (loads config.yaml)
├── config.yaml                    # Non-secret settings (providers, models, server)
│
├── core/                          # Internal libraries (not entry points)
│   ├── secrets.py                # Shared get_credential() utility
│   ├── keyring_backend.py        # Keyring backend for containerised minds
│   ├── gateway_client.py         # Shared HTTP client for bots → hive-comms
│   ├── notify_utils.py           # Shared Telegram notification utility
│   ├── path_validation.py        # CWE-22 path traversal protection for skill agents
│   ├── scheduled_skills.py       # Scheduler-driven skill runs
│   ├── skill_telemetry_detect.py # Skill usage telemetry
│   ├── story_pipeline.py         # Post-merge story pipeline (pull, health check, cleanup)
│   └── training_capture*.py      # Per-harness training-turn capture (Claude, Codex)
│
├── tools/
│   └── stateless/                 # Standalone scripts (invoked via skills)
│       ├── crypto/crypto.py      # CoinGecko crypto prices
│       ├── weather/weather.py    # Open-Meteo weather
│       ├── notify/notify.py      # Telegram/email notifications
│       ├── reminders/reminders.py # One-time reminders (SQLite)
│       ├── secrets/secrets.py    # Keyring secret management
│       ├── x_api/x_api.py       # X/Twitter search
│       ├── agent_logs/agent_logs.py # Log file scanner
│       ├── current_time/current_time.py # Timezone-aware clock
│       └── poll_broker/poll_broker.py # Polls broker for inter-mind task results (stdlib only)
│
├── bots/                          # Thin client entry points
│   ├── discord_bot.py            # Discord bot
│   ├── telegram_bot.py           # Telegram bot (Ada + named minds)
│   ├── hivemind_bot.py           # Group chat Telegram bot (multi-mind sessions)
│   └── scheduler.py              # Cron daemon
│
├── voice/                         # Voice infrastructure
│   └── voice_server.py           # STT/TTS FastAPI server
│
├── docs/                          # Human-readable documentation and background
├── jobs/                          # Data files (resumes, specs)
├── data/                          # SQLite databases (Docker volume)
│
├── minds/                         # Minds: shared harness code + per-deployment folders
│   ├── mind_server.py            # The in-container service: every harness adapter, routed per session
│   ├── transcript.py             # Harness transcripts rendered as a plain-text handover
│   ├── harness/                  # Harness adapters: claude_cli.py, codex_cli.py, dsh_cli.py
│   ├── proactive.py              # Shared unsolicited-delivery plumbing
│   ├── pty_attach.py             # Shared tmux-backed browser terminal (docs/architecture/browser-terminal.md)
│   ├── example/                  # Tracked starter mind (runtime.yaml + compose fragment)
│   └── <name>/                   # Deployment minds (gitignored): runtime.yaml, prompts, .claude/.codex, container/
│
├── souls/                         # Per-mind identity seed files (one-time use only)
│   ├── ada.md                    # Ada's soul seed
│   ├── bilby.md                  # Bilby's soul seed
│   ├── bob.md                    # Bob's soul seed
│   ├── nagatha.md                # Nagatha's soul seed
│   └── skippy.md                 # Skippy placeholder
│
├── utilities/                     # Standalone utilities (not invoked via skills)
│   └── ollama_tools.py           # Direct Ollama API client
│
├── vendor/                        # Vendored dependencies
│   └── claude_code_sdk/          # Vendored Claude Code SDK (legacy/template support)
│
├── plans/                         # Forward-looking plans and proposals (not yet implemented)
│
├── soul.md                        # Pointer stub (see souls/ada.md)
├── CLAUDE.md                      # This file
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Configuration

Non-secret settings in `config.yaml`:

```yaml
server_port: 8420
max_sessions: 10
default_model: claude-sonnet-5

providers:
  anthropic: {}
  ollama:
    env:
      ANTHROPIC_AUTH_TOKEN: "ollama"
      ANTHROPIC_BASE_URL: "http://<ollama-host>:11434"
    api_base: "http://<ollama-host>:11434"

```

Secrets are stored in the system keyring (`keyrings.alt.file.PlaintextKeyring`).
Use `get_credential()` from `core/secrets.py` to read them.

### How the gateway authenticates itself to a mind

Comms itself is gated on the way in — a bearer check on every HTTP route, an
admin bearer on top for `/broker/minds` writes. On the way *out*, every call it
makes to a mind carries that mind's own credential.

| Layer | Owner | Lifetime |
|---|---|---|
| `minds/<name>/session_token` (0600) | the mind, on disk | durable truth |
| `broker.minds.session_token` | comms | a cache of the above |

The mind mints the token on first use and publishes it in the boot
registration it already performs — the same admin-guarded upsert that carries
its model and address. Comms reads it per call through
`broker.get_mind_session_token`, the one accessor that returns it; every mind
listing goes through `_public_mind`, which strips the column, because those
listings answer to the service token every surface bot holds. One token taken
off one mind opens that mind and no other, which matters most for the minds
trusted least — the two Windows boxes the boys use.

The admin token is deliberately *not* the credential on this path: it unlocks
`PATCH /runtime`, the skills write-back and the file editor, and a routine chat
turn must not carry the thing that owns the machine. A mind still accepts it,
so the console or the operator can reach a wedged session directly.

All seven outbound paths carry it — spawn, message, interrupt, release,
rotate-pty, kill, and the terminal WebSocket proxy — and **a refusal is
reported as a refusal on each**. A 401 or 503 is never folded into the shape
that means something else: a mind that is down, a mind with nothing to
release, a mind holding no live terminal, a mind offering no models. Three of
those are remedies applied to the wrong machine. Two of them were worse than
cosmetic: a refused release read as "nothing to release" let a cross-surface
adoption retarget ownership over a harness that was still running — two
processes on one transcript — and a refused kill read as success left a tmux
session and its context alive until the box rebooted. The terminal tile closes
on **4416** rather than the 4415 that means "no pty route in this image".

On the mind side (`minds/runtime_api.py`, mounted by the mind server) one
middleware guards every `/sessions` route, reading `scope["path"]` and not
`request.url.path` — Starlette builds that URL from the *Host header*, so a
Host carrying a `/` moves the route out of `.path` while the router still
matches it. Credentials compare as bytes, since `compare_digest` raises
`TypeError` on non-ASCII `str` and that 500 reaches the gateway as "no
terminal route". A mind that cannot establish its own credential answers
**503**, never open: an unreadable token file is not an absent one, and
treating it as absent serves the whole session surface to the LAN while the
gateway keeps presenting a token nobody checks.

**The rollout direction is one-way.** Comms can start sending a credential
with no risk to anything — a mind that does not check one ignores it. A mind
cannot start requiring one before comms sends it. So comms first, then minds
one at a time; deploying the code to a mind is the act that flips it.

**Two caveats, stated rather than papered over.** Per-mind isolation is real
between *hosts* — the kid boxes, Hex, Dragoman, this workstation — and weaker
than the phrase suggests in two places. The five container minds all
bind-mount the same tree read-write, so each can read
`minds/<other>/session_token`; injecting `MIND_SESSION_TOKEN` per container
from its own env file fixes that. And the guard accepts the admin token as
well as the session token, deliberately, so the console and the operator can
reach a wedged pane — but on this hive that resolves to
`COMMS_ADMIN_BEARER_TOKEN`, which every mind already holds. So a compromised
mind can still reach another's session surface with a credential it had
before; what it cannot do any more is reach one with nothing at all. A
distinct `MIND_ADMIN_TOKEN` per mind closes that, and is the next thing worth
doing if the boys' boxes stop being trusted.

### Which model a session runs on

Three layers, each with one owner:

| Layer | Owner | Lifetime |
|---|---|---|
| `minds/<name>/runtime.yaml` → `default_model` | the mind, on disk | durable truth |
| `broker.minds.model` | comms | a cache of the above |
| `sessions.model` | comms | one conversation's snapshot |

Every mind re-registers from its own `runtime.yaml` on start
(`minds/runtime_api.py` → `POST /broker/minds`, an upsert on `mind_id`), so
the broker row converges on the file rather than drifting from it. The same
module serves `GET`/`PATCH /runtime`, which is how the console edits a mind's
default — the file first, the broker row second, over HTTP for every mind
including the ones on other machines.

`create_session` resolves `caller-supplied model || broker.minds.model`, and
raises when it has neither. Nothing below it defaults: a mind handed a spawn
or an `attach-pty` with no model refuses. A rotation passes the retiring
session's own model explicitly, so a `/model` switch survives it and a
changed default cannot reach into a live conversation — that default is for
the next one.

### What a conversation is called

A conversation's name and colour are columns on its `sessions` row, and that
row is the only place either lives. `summary` sits beside them and is a
different thing: a preview generated once from the first chat message, used
only when there is no name. A name is given; a preview is computed.

The name follows the conversation across a rotation. `create_session` copies
it off the predecessor when `rotated_from` is set, so a successor is born
wearing it — copied at creation rather than read through the link, because the
predecessor is retired moments later. The in-place terminal rotation keeps its
row, so nothing to carry.

`PUT /sessions/{id}/name` is a **partial** write: an absent field is unchanged,
an empty one clears. A whole-record write is how a caller that only knew about
names blanked the colour picked at a browser tile. A rename addressed to a
closed session is refused with 409 rather than committing to a row nothing
reads — the ids come from picker buttons and rename prompts that deliberately
never expire.

The route carries `X-Rename-Token` (`COMMS_RENAME_TOKEN`, or the admin token)
on top of the service bearer, and **refuses when neither is configured**. The
service token is held by every surface bot and every mind container on the
hive; reading a name is not worth guarding, changing one is. Reads ride
`GET /sessions` and `GET /sessions/names` on the service token alone.

`comms/label_migration.py` is the one-time move from the browser terminal's
old `terminal_labels` table, walking each rotation chain forward so a stranded
name lands on the conversation that continued it. Idempotent by rule rather
than by marker: a target already carrying a name is never written over.

## Gateway API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/sessions` | Create session |
| `GET` | `/sessions` | List sessions |
| `GET` | `/sessions/names` | Every conversation's name and colour |
| `GET` | `/sessions/{id}` | Get session detail |
| `PUT` | `/sessions/{id}/name` | Name or recolour a conversation (rename token) |
| `DELETE` | `/sessions/{id}` | Kill session |
| `POST` | `/sessions/{id}/message` | Send message (SSE streaming) |
| `POST` | `/sessions/{id}/activate` | Activate session on a surface |
| `POST` | `/sessions/{id}/model` | Switch model mid-session |
| `POST` | `/sessions/{id}/autopilot` | Toggle autopilot |
| `WS` | `/sessions/{id}/stream` | WebSocket bidirectional |
| `GET` | `/models` | List available models |
| `POST` | `/command` | Route slash commands |
| `POST` | `/sessions/{id}/remote-control` | Start remote observation of a session |
| `DELETE` | `/sessions/{id}/remote-control` | Stop remote observation |
| `POST` | `/group-sessions` | Create group session (multi-mind) |
| `GET` | `/group-sessions/{id}` | Get group session detail |
| `POST` | `/group-sessions/{id}/message` | Send message to group session |
| `DELETE` | `/group-sessions/{id}` | Kill group session |
| `POST` | `/memory/expiry-sweep` | Trigger timed-event expiry sweep |
| `POST` | `/epilogue/sweep` | Trigger session epilogue sweep |
| `POST` | `/hitl/request` | Submit HITL approval request |
| `GET` | `/hitl/status/{token}` | Check HITL approval status |
| `POST` | `/hitl/respond` | Respond to HITL approval request |
| `POST` | `/broker/messages` | Send inter-mind message (async, returns immediately, wakes callee in background) |
| `GET` | `/broker/messages` | Query messages by `conversation_id` (polling) |
| `GET` | `/broker/conversations/{id}` | Get conversation with all messages |
| `GET` | `/broker/minds` | List all registered minds |
| `POST` | `/broker/minds` | Register a mind |
| `PUT` | `/broker/minds/{name}` | Update mind fields |
| `DELETE` | `/broker/minds/{name}` | Deregister a mind |

## Adding New Tools

Use the `/tool-creator` skill, which reads `specs/tool-migration.md` to determine the right pattern. Preferred is **stateless** — a standalone script wired via a Claude skill:

- Create `tools/stateless/<name>/<name>.py` with argparse + JSON stdout
- Create a Claude skill in `.claude/skills/<name>/SKILL.md` to invoke it
- Editable without any container restart

If a tool genuinely needs a persistent connection (e.g., a long-lived browser session), it can become a small FastAPI service reached over HTTP — same pattern as `hive-lucent` and `hive-tools`.

## Key Design Principles

1. **Claude Code does the heavy lifting** — don't reimplement what it does natively
2. **Tools return raw data** — no LLM formatting layers; the model formats
3. **Self-improvement via tool creation** — new capabilities generated on demand
4. **Less code is better** — if Claude Code already does it, don't wrap it
5. **Gateway is the single source of truth** — all clients go through server.py
6. **Per-process isolation** — env vars set per subprocess, never globally
7. **Always echo directory paths exactly** — whenever a directory path is mentioned (by either party), spell it out character-for-character as you understand it (e.g. `nervous-system`, not "nervous system") so Daniel can catch hyphen/underscore/casing errors before any action is taken.
