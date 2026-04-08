# Implementation Plan: Mind-to-Mind Messaging — Phase 2 (MIND.md Migration)

> **Status:** Not yet planned. Depends on Phase 1 completion.
>
> **Spec:** See "Phase 2+" section of `plans/llm-messaging-architecture.md`

## Scope

- MIND.md migration (replacing `config.yaml` minds section + `souls/` directory)
- `cli_harness.py` elimination (inline into each implementation.py)
- Filesystem-driven mind registration
- `sessions.py` decoupled from `config.yaml` minds section
- Mind CRUD skills (create-mind, update-mind, remove-mind, add-mind, list-minds)

## Prerequisite

Phase 1 (`IMPLEMENTATION-phase1.md`) must be complete and merged.

## Planning

Run `/planning-genius` against this file after Phase 1 ships.
