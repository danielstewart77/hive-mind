# Nervous System (Lucent)

The vector store + knowledge graph that backs every mind's memory lives in a separate, shared service:

**👉 [github.com/danielstewart77/hive_nervous_system](https://github.com/danielstewart77/hive_nervous_system)**

This repo (`hive_mind`) contains only the **consumer side**:

- `core/lucent_client.py` — HTTP+bearer wrapper, the single Python entry point for memory + graph writes from hive_mind code.
- `minds/_shared/hooks/` — the four bash hooks each mind installs (capture, bootstrap, retrieval, rotation).
- `minds/_shared/scripts/` — `/remember` and `/always-remember` skill backends.

## Network wiring

`hive-lucent` joins the external `hivemind` Docker network. Mind containers reach it as `http://hive-lucent:8424`. The shared container also exposes `127.0.0.1:8425` on the host for direct curl/debugging and for any bare-metal consumer (e.g., the `hive_mind_skippy` standalone mind).

## Bearer auth

`LUCENT_BEARER_TOKEN` is set in the host's `.env` and propagated into every mind container via compose `env_file`. Hooks source the same env. Empty token = bypass mode with a startup warning (deployment safety).

## Identity convention

Every write to lucent uses the **canonical mind id** (`MIND_AGENT_ID`), not the human-readable short name. For registry-managed minds this is a UUID issued by `core/sessions.py`. For unmanaged/bare-metal minds it's a stable literal string.

`MIND_ID` (`ada`, `bob`, …) is for log paths, container names, and display only — **never written to lucent**.

See the [implementation playbook](https://github.com/danielstewart77/hive_nervous_system/blob/main/docs/memory-system-implementation.md#identity-convention) for the full convention and recovery recipe.

## Full design + spec

| Document | Scope |
|---|---|
| [memory-system-design.md](https://github.com/danielstewart77/hive_nervous_system/blob/main/docs/memory-system-design.md) | Mind-agnostic architecture: rotation, four-layer bootstrap, capture pipeline, pruning, graph query semantics |
| [memory-system-implementation.md](https://github.com/danielstewart77/hive_nervous_system/blob/main/docs/memory-system-implementation.md) | Adopter playbook. Per-harness hook coverage (Claude CLI, Claude SDK, Codex CLI), env, identity convention, verification checklist, "Constraints (don't relearn)" |
| [memory-system-requirements.md](https://github.com/danielstewart77/hive_nervous_system/blob/main/docs/memory-system-requirements.md) | 84 verifiable requirements |

## Inter-mind / broker

`nervous_system/inter_mind_api/` (broker, group-chat, mind-to-mind delegation) still lives in this repo. A follow-up will move it into `hive_nervous_system` so the entire shared infrastructure lives in one place — see the in-progress backlog item.
