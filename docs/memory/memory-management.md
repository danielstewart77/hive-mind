# Memory Management

**Replaced.** The 4-step manifest-chain orchestrator (parse-memory → classify-memory → route-memory → save-memory) was retired in the F13 / memory-system Phase 1 cutover. Memory is now captured by a **per-turn Stop hook** that classifies and writes in a single pass — no orchestrator, no subagent triad, no manifests.

For the current architecture, read in this order:

1. **[architecture/nervous-system.md](../architecture/nervous-system.md)** — local consumer-side overview
2. **[memory-system-design.md](https://github.com/danielstewart77/hive_nervous_system/blob/main/docs/memory-system-design.md)** — full mind-agnostic design
3. **[memory-system-implementation.md](https://github.com/danielstewart77/hive_nervous_system/blob/main/docs/memory-system-implementation.md)** — adopter playbook (per-harness hook coverage, env, identity convention)
4. **[memory-system-requirements.md](https://github.com/danielstewart77/hive_nervous_system/blob/main/docs/memory-system-requirements.md)** — 84 verifiable requirements

The four data classes (`ephemeral`, `current-state`, `future-state`, `feedback`) are documented under [`specs/data-classes/`](../../specs/data-classes/) — `index.md` is the entry point.
