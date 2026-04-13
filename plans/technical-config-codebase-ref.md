# Plan: Enforce codebase_ref on technical-config Memories

> **Status:** Not yet implemented. Discovered during first prune-config-memory run (2026-04-12).
> **Background:** All ~180 existing technical-config entries have codebase_ref: null, which blocks the Step 2b verification path in the prune-config-memory skill. Old entries will age out naturally; the fix is forward-only.

---

## Goal

Ensure all future technical-config memory saves include a `codebase_ref` whenever the fact references a specific file, class, or function. This makes the `prune-config-memory` skill's verification pass actually useful.

---

## Step 1: Tighten the Data Class Spec

Update `specs/data-classes/technical-config.md`:

- Change the `codebase_ref` note from "REQUIRED when the fact references specific files" (passive) to an explicit list of what triggers a required ref
- Add examples of what DOES and DOES NOT require a codebase_ref:

  **Requires codebase_ref:**
  - "The `_split_sentences()` function in `voice/voice_server.py`..."
  - "A new `get_sessions_pending_epilogue()` method was added to `core/sessions.py`..."
  - "The `step-coding` agent is at `.claude/agents/step-coding.md`..."

  **Does NOT require codebase_ref (high-level, no code anchor):**
  - "The memory pipeline uses a manifest chain..."
  - "Chatterbox voice server is confirmed working..."
  - "LinkedIn API cannot read posts..."

- Add: "If you are unsure whether to include codebase_ref, err on the side of including it."

---

## Step 2: Update the classify-memory Agent

Update `minds/ada/.claude/agents/classify-memory.md`:

After a chunk is classified as `technical-config`, add a check:

> "Does the content reference a specific file path, class name, or function name? If yes, extract these references and record them as the `codebase_ref` value (comma-separated paths/symbols). If no specific code location is referenced, leave codebase_ref empty."

This should run as part of the classification step, not as a separate pass — the classifier already reads the content to determine the class, so it has the context needed to extract refs.

---

## Step 3: Update the save-memory Agent

Update `minds/ada/.claude/agents/save-memory.md`:

For chunks routed to `save-vector` with `data_class: technical-config`:

- Before calling `memory_store`, check if `codebase_ref` is populated
- If not populated and the content references a specific file/function: prompt to extract it before saving
- If not populated and the content is genuinely high-level: proceed without it

This is a secondary safety net — classify-memory should catch it first.

---

## Step 4: Update the technical-config Data Class Notes

Add a note to `specs/data-classes/technical-config.md` under Notes:

> "The prune-config-memory skill uses codebase_ref to verify entries against live code. Entries without codebase_ref can only be assessed for plausibility, not verified. Always include codebase_ref where a code location exists."

---

## Step 5: (Optional) Targeted Backfill Pass

For the ~30-40 existing entries that clearly name a file or function in their content, a targeted backfill run could add codebase_refs retroactively. This is lower priority than the forward fixes above.

Candidates for backfill (entries where content mentions a specific file):
- voice/voice_server.py entries (chunked synthesis, sentence splitting)
- core/sessions.py entries (epilogue methods, lock mechanism)
- core/epilogue.py entries (Phase 3 rewrite)
- core/gateway_client.py entries (queue drain logic)
- tools/stateless/planka/planka.py entries
- clients/telegram_bot.py entries

Approach: run memory_list with data_class: technical-config, scan content for file path patterns (`\w+/\w+\.py`), update matching entries with extracted codebase_ref.

This could be automated as an additional step in the prune-config-memory skill.
