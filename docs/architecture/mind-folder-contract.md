# Mind Folder Contract

Every mind is a self-contained folder under `minds/`. The hive picks up any folder named anything — `ada/`, `bob/`, `george_esquire_the_third/` — and runs it as a containerized mini-service. There is no central "mind registry file" to edit when adding a mind. Drop the folder, add it to `docker-compose.yml`'s `include:` list, and rebuild.

## Layout

```
minds/<any_name>/
├── runtime.yaml              # operational config (mind_id UUID, harness, model, env, prompts)
├── prompts/                  # per-mind prompt fragments (common.md, harness.md, profile.md)
├── .claude/                  # for Claude CLI / SDK minds
│   ├── settings.json         #   declares Stop / SessionStart / UserPromptSubmit hooks
│   └── hooks/                #   per-mind hook scripts (one copy per mind, no sharing)
├── .codex/                   # for Codex CLI minds (instead of .claude)
│   ├── config.toml           #   declares hooks via [[hooks.X]] blocks
│   └── hooks/                #   identical scripts
├── data/auto-remember/       # capture/soul/rotation/bootstrap logs (volume-mounted)
└── container/
    └── compose.yaml          # per-mind container spec (image, env, volumes, network)
```

## The four invariants

1. **Mind containers are full mini-services scoped to one mind.** Not "dumb sandboxes." Each runs the shared mind server (`minds/mind_server.py`) as its sole entry point — the claude, codex and dsh adapters in one process, each conversation routed to the harness it names — pointed at this folder via `MIND_NAME`. No per-mind service code.
2. **The folder is the unit of configuration and deployment.** No central directory of minds; a folder is the registration.
3. **Gateway is the hive's only public-facing surface.** Everything else is internal to a mind.
4. **Identity is the canonical UUID, not the short name.** `runtime.yaml` carries `mind_id: <uuid>`. The container ships `MIND_ID=<uuid>` for everything that talks to shared infrastructure and `MIND_NAME=<short>` for display, log paths, and the capitalized entity name used in graph queries.

## Per-harness hook registration

The three hooks that carry capture, retrieval, and rotation
(`auto_remember.sh`, `contextual_retrieval.sh`, `rotation_check.py`)
live in each mind's own hook directory — Claude-CLI minds under
`minds/<name>/.claude/hooks/`, Codex-CLI minds under
`minds/<name>/.codex/hooks/`. The script bodies are byte-identical
across minds today, but every mind owns its own copy so a change to
one mind's hooks never affects another. Each mind's harness loads
them differently:

| Harness | Config file | How |
|---|---|---|
| **Claude CLI** (Ada, Bob) | `.claude/settings.json` | Native — JSON `hooks` block, scripts loaded from `minds/<name>/.claude/hooks/` |
| **Codex CLI** (Bilby, Nagatha) | `.codex/config.toml` | `[features] codex_hooks = true` + `[[hooks.X]]` blocks, scripts loaded from `minds/<name>/.codex/hooks/` |

All hooks emit the same JSON output schema (`systemMessage`, `continue`, `suppressOutput`, `stopReason`) so the scripts themselves are identical across harnesses.

See [`per-mind-hooks.md`](per-mind-hooks.md) for the per-event breakdown
of what each hook does.

## Required env per mind

Every mind container must have these env vars set (via compose `environment:` + `env_file:`):

```
MIND_ID=<canonical_uuid>                 # 565e5a66-… — written to lucent's mind_id column
MIND_NAME=<short_name>                   # ada, bob, bilby, nagatha — display, log paths, entity-name source
LUCENT_URL=http://hive-lucent:8424       # vector store + KG
LUCENT_URL_SELF=http://hive-lucent:8424  # alias used by some hook scripts
LUCENT_BEARER_TOKEN=${LUCENT_BEARER_TOKEN}
HIVE_TOOLS_URL=http://hive-tools:9421
HIVE_TOOLS_TOKEN=${HIVE_TOOLS_TOKEN}
COMMS_URL=http://hive-comms:8424         # NS gateway — composes prompts, dispatches sessions, receives rotation memory
COMMS_BEARER_TOKEN=${COMMS_BEARER_TOKEN}
SPECS_DIR=/usr/src/app/specs/data-classes
AUTO_REMEMBER_LOG_DIR=/usr/src/app/minds/${MIND_NAME}/data/auto-remember
```

The bearer tokens come from the host-level `.env` and are interpolated by Docker Compose at container start.

### One key per mind

A mind has one key, the value its owner pasted when it was made, and presents
it to every hive service as that mind: the inference proxy, the gateway, the
memory store and the tools. The key is a FILE, not an environment value: one
folder per mind, `<key root>/<name>/key`, kept outside the checkout (every mind
mounts the checkout, so a key inside it would be readable by every mind) and
mounted read-only into that mind alone. Its compose fragment names two things:

```
MIND_KEY_FILE=/run/mind-key/key                 # where the mind reads its key
${HOST_KEY_DIR}/<name>:/run/mind-key:ro         # a volume: its own folder, and no other
MIND_PROACTIVE_TOKEN=${PROACTIVE_TOKEN_<NAME>}  # its own bots' credential, never the key
```

The console is the only writer: it holds the key root read-write
(`HIVE_KEY_ROOT`) and replaces the file by writing a temporary file in the same
folder, fsyncing it and renaming it over the old one, so a reader sees the old
key or the new one and never half of one. A FOLDER is mounted because a
single-file bind mount cannot be replaced atomically. The key is in no `.env`
and in no container creation setting (so not in `docker inspect`).

The mind re-reads the file at each request and at each spawn
(`core.mind_key.refresh`: one `stat`, the file read only when it changed) and
sets every per-service name from it, so a replaced key is the mind's key on its
next request with no restart and no recreate. A file that is not there when the
mind starts falls back to `MIND_KEY` in its environment (the transition for a
mind made before key files). After a good key has been read, an unreadable,
garbled (not the shape of a key) or empty file keeps the last good key and is
logged once, never with the key. A turn already running, a terminal session
already open and a hook process already started keep the key they were given
until they end; new ones get the new key.

A mind on another machine has no console that can write its folder: a person
places the key file there (mode 0600, in a folder mounted or readable by that mind
only) and sets `MIND_KEY_FILE` to it.

At start and at each refresh `core.mind_key.apply_environment` sets
`COMMS_BEARER_TOKEN`, `HIVEMIND_BROKER_TOKEN`, `LUCENT_BEARER_TOKEN`,
`HIVE_TOOLS_TOKEN`, `MIND_PROXY_KEY`, `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`
and `DSH_API_KEY` from it, so the harness binaries and the per-mind hook copies
(which read those names) are unchanged. A mind started with neither a key file
nor `MIND_KEY` keeps the per-service variables above: that is the transition,
until a mind's key is moved and the old values are retired. A `runtime.yaml` `env:` block is not where
a key lives; with a key set, any key-bearing name in it is ignored, and
the inference names above are the mind's key, not a place for an outside
credential (keep one of those under another name). To
replace a mind's key, paste the new one on the mind's page: the console writes
the file and the mind takes it at its next request. A mind made before key files
is recreated once to add the mount; after that no replacement needs a recreate.

Surfaces (Telegram, Discord, web) reach a mind's backend through
`hive-comms` rather than the mind container directly:
`HIVE_MIND_SERVER_URL=http://hive-comms:8424`. `comms` is registered as
the broker's mind-resolution layer; it composes `system_prompt_blocks`
(soul + standing + decay-weighted recent + session-memory
carry-forward) and ships them to each mind in the dispatch payload.
See [`per-mind-hooks.md`](per-mind-hooks.md) and the upstream NS
[`session-prompt-composition.md`](../../nervous-system/docs/session-prompt-composition.md)
for the full contract.

## Identity-node guard

The KG identity node for each mind must be `type='Mind'` (not `Person` or `Agent`). The guard in `lucent_graph.py` keys on `type='Mind'` to enforce that only the mind whose `mind_id` matches can edit its own identity row. Misclassifying the type silently disables the guard.

See [Lucent design](nervous-system.md) and the full [implementation playbook](../../nervous-system/docs/memory-system-implementation.md) for end-to-end adoption.
