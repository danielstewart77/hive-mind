# Data Class: technical-config

## Description
A stable technical fact about how the Hive Mind system is built, configured, or operates. Recognizable by references to system components, architecture decisions, pipeline designs, file locations, or build events — things that define what the system is or how it works.

## Actions
- `save-vector`

## Required Fields
- `codebase_ref`: REQUIRED when the fact references specific files, classes, or functions. Set to a comma-separated list of file paths or symbols (e.g. `core/hitl.py,server.py`, `SessionManager.send_message`). Most config facts span multiple files — list all relevant ones. Used by the pruning agent to verify the fact is still accurate. Omit only for high-level architectural facts with no code location.

## Notes
- Covers both design decisions ("the memory pipeline uses a manifest chain") and build events ("the memory pipeline was built on 2026-03-05")
- Does not include transient session chatter — only facts that remain true and useful beyond the session
- If a technical fact is so foundational it needs to be in every conversation context, use `pin-memory` instead
