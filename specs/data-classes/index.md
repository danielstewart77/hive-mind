# Data Class Index

This file lists every available memory data class and its spec file.
It is the first thing the classifier reads before matching any chunk.

The classifier evaluates the three storage classes (`current-state`,
`future-state`, `feedback`) first. If none match, the chunk falls
through to `ephemeral`.

---

## Available Data Classes

| Class Name | Spec File | Storage Bucket | Description |
|------------|-----------|----------------|-------------|
| `ephemeral` | `ephemeral.md` | — | Fall-through for chunks that don't match any other class — always discarded |
| `current-state` | `current-state.md` | both | Durable facts about the present state of the system, codebase, people, or minds |
| `future-state` | `future-state.md` | both | Planned, intended, or designed things not yet shipped |
| `feedback` | `feedback.md` | vector | Preferences, corrections, behavioral rules — anything that should shape future behavior. Standing-tier subset written via `/always-remember` |
