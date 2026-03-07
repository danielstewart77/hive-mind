# Data Class: technical-config

## Description
A stable technical fact about how the Hive Mind system is built, configured, or operates. Recognizable by references to system components, architecture decisions, pipeline designs, file locations, or build events — things that define what the system is or how it works.

## Actions
- `save-vector`

## Required Fields
- `codebase_ref`: REQUIRED when the fact references a specific file, class, or function. Set to the file path or symbol (e.g. `core/hitl.py`, `SessionManager.send_message`). Used by the pruning agent to verify the fact is still accurate. Omit only for high-level architectural facts with no single code location.

## Notes
- Covers both design decisions ("the memory pipeline uses a manifest chain") and build events ("the memory pipeline was built on 2026-03-05")
- Does not include transient session chatter — only facts that remain true and useful beyond the session
- If a technical fact is so foundational it needs to be in every conversation context, use `pin-memory` instead
