# Mind Folder Contract

Every mind is a self-contained folder under `minds/`. The hive picks up any folder named anything — `ada/`, `bob/`, `george_esquire_the_third/` — and runs it as a containerized mini-service. There is no central "mind registry file" to edit when adding a mind. Drop the folder, add it to `docker-compose.yml`'s `include:` list, and rebuild.

## Layout

```
minds/<any_name>/
├── runtime.yaml              # operational config (mind_id UUID, harness, model, env, prompts)
├── implementation.py         # spawn / send / kill — typically self-contained, ~100–170 lines
├── prompts/                  # per-mind prompt fragments (common.md, harness.md, profile.md)
├── .claude/                  # for Claude CLI / SDK minds
│   ├── settings.json         #   declares Stop / SessionStart / UserPromptSubmit hooks
│   └── hooks/                #   per-mind hook scripts (or symlinks to minds/_shared/hooks/)
├── .codex/                   # for Codex CLI minds (instead of .claude)
│   ├── config.toml           #   declares hooks via [[hooks.X]] blocks
│   └── hooks/                #   identical scripts
├── data/auto-remember/       # capture/soul/rotation/bootstrap logs (volume-mounted)
└── container/
    └── compose.yaml          # per-mind container spec (image, env, volumes, network)
```

## The four invariants

1. **Mind containers are full mini-services scoped to one mind.** Not "dumb sandboxes." Each runs the mind's own `implementation.py` directly as its sole entry point — no separate "mind server" intermediary.
2. **The folder is the unit of configuration and deployment.** No central directory of minds; a folder is the registration.
3. **Gateway is the hive's only public-facing surface.** Everything else is internal to a mind.
4. **Identity is the canonical UUID, not the short name.** `runtime.yaml` carries `mind_id: <uuid>`. The container ships `MIND_ID=<short>` for display and `MIND_AGENT_ID=<uuid>` for everything that talks to shared infrastructure.

## Per-harness hook registration

The same four bash hooks (`auto_remember.sh`, `session_start_bootstrap.sh`, `contextual_retrieval.sh`, `rotation_check.sh`) are shared at `minds/_shared/hooks/`. Each mind's harness loads them differently:

| Harness | Config file | How |
|---|---|---|
| **Claude CLI** (Ada, Bob) | `.claude/settings.json` | Native — JSON `hooks` block |
| **Claude SDK** (Bilby) | `.claude/settings.json` | Loaded via `ClaudeCodeOptions(settings="<path>")` |
| **Codex CLI** (Nagatha) | `.codex/config.toml` | `[features] codex_hooks = true` + `[[hooks.X]]` blocks |

All hooks emit the same JSON output schema (`systemMessage`, `continue`, `suppressOutput`, `stopReason`) so the scripts themselves are identical across harnesses.

## Required env per mind

Every mind container must have these env vars set (via compose `environment:` + `env_file:`):

```
MIND_ID=<short_name>                     # ada, bob, bilby, nagatha — display only
MIND_AGENT_ID=<canonical_uuid>           # what gets written to lucent
MIND_NAME=<display_name>                 # Ada, Bob, Bilby, Nagatha — used by hooks for graph queries
LUCENT_URL=http://hive-lucent:8424
LUCENT_BEARER_TOKEN=${LUCENT_BEARER_TOKEN}
HIVE_TOOLS_URL=http://hive-tools:9421
HIVE_TOOLS_TOKEN=${HIVE_TOOLS_TOKEN}
GATEWAY_URL=http://server:8420
SESSIONS_DB_PATH=/usr/src/app/data/sessions.db
SPECS_DIR=/usr/src/app/specs/data-classes
AUTO_REMEMBER_LOG_DIR=/usr/src/app/minds/${MIND_ID}/data/auto-remember
```

The bearer tokens come from the host-level `.env` and are interpolated by Docker Compose at container start.

## Identity-node guard

The KG identity node for each mind must be `type='Mind'` (not `Person` or `Agent`). The guard in `lucent_graph.py` keys on `type='Mind'` to enforce that only the mind whose `agent_id` matches can edit its own identity row. Misclassifying the type silently disables the guard.

See [Lucent design](nervous-system.md) and the full [implementation playbook](https://github.com/danielstewart77/hive_nervous_system/blob/main/docs/memory-system-implementation.md) for end-to-end adoption.
