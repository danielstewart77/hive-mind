# Nervous System (Lucent + Comms)

The shared data and signal plane — lucent (vector store + knowledge
graph) and comms (the gateway) — lives in-repo:

**👉 [`nervous-system/`](../../nervous-system/)** — see its README for
layout, endpoints, and how to run the test suites.

The rest of this repo is the **consumer side**:

- `core/gateway_client.py` — the bots' HTTP client for hive-comms.
- `minds/<name>/.claude/hooks/` (or `.codex/hooks/`) — the per-turn hook scripts each mind owns its own copy of: capture, retrieval, rotation. They call lucent directly with curl + bearer.

## Network wiring

`hive-lucent` and `hive-comms` join the external `hivemind` Docker network. Mind containers reach them as `http://hive-lucent:8424` and `http://hive-comms:8424`. Both also bind host ports — `127.0.0.1:8425` (lucent) and `127.0.0.1:8426` (comms) — for direct curl/debugging and for any bare-metal consumer (e.g., the `hive_mind_skippy` standalone mind).

## Bearer auth

`LUCENT_BEARER_TOKEN` and `COMMS_BEARER_TOKEN` are set in `nervous-system/.env` and propagated into consumers via compose `env_file`. Hooks source the same env. Empty token = bypass mode with a startup warning (deployment safety).

## Identity convention

Every write to lucent uses the **canonical mind id** (`MIND_ID`), a UUID issued by `nervous-system/comms/sessions.py` for registry-managed minds and a stable literal string for unmanaged/bare-metal minds.

`MIND_NAME` (`ada`, `bob`, …) is for log paths, container names, and the capitalized entity name used in graph queries — **never written to lucent's `mind_id` column**.

See the [implementation playbook](../../nervous-system/docs/memory-system-implementation.md#identity-convention) for the full convention and recovery recipe.

## Full design + spec

| Document | Scope |
|---|---|
| [memory-system-design.md](../../nervous-system/docs/memory-system-design.md) | Mind-agnostic architecture: rotation, four-layer bootstrap, capture pipeline, pruning, graph query semantics |
| [memory-system-implementation.md](../../nervous-system/docs/memory-system-implementation.md) | Adopter playbook. Per-harness hook coverage (Claude CLI, Claude SDK, Codex CLI), env, identity convention, verification checklist, "Constraints (don't relearn)" |
| [memory-system-requirements.md](../../nervous-system/docs/memory-system-requirements.md) | 84 verifiable requirements |
