# Memory Management — Orchestrator Design

> Working document. Brainstormed with Daniel on 2026-03-05.

---

## Goal

A `memory-manager` skill that orchestrates the full lifecycle of memory in the Hive Mind system — deciding what to store, how to store it, when to review it, and when to prune it.

Modeled after the `orchestrator` skill for software development: high-level skill drives a series of named sub-agents, each responsible for a specific phase.

---

## High-Level Orchestrator: `memory-manager`

**Triggers:**
- User says "remember this" (single item)
- New session starts (full transcript)

**Five steps — in order:**

1. **Parse** — Determine content scope, clarify if needed, write to temp file
2. **Classify** — Review each chunk; assign type and subtype
3. **Route** — If class is known → prescribed action; if unknown → hold and notify Daniel
4. **Save** — Execute the upsert/storage procedure for each classified chunk

---

## Sub-Agents

### 1. `parse-memory`

**Responsibility:** Pull out the content to be remembered and write it to a **chunk manifest** — one document listing all memory candidates as discrete chunks.

**Trigger path A — manual ("remember this"):**
- Determine if user wants to remember the entire thread or a specific data item
- If unclear → prompt the user to clarify
- Once confirmed → write chunks to manifest

**Trigger path B — automated (new session):**
- Input is always the full transcript — no ambiguity, no user prompt needed
- Parse transcript into chunks and write to manifest

**Returns:** `pass` if chunk manifest created successfully, `fail` otherwise.

---

### 2. `classify-memory`

**Responsibility:** Match each memory chunk to a data class specification. No hardcoded logic — all classification rules live in spec files.

**Step 1 — Read the index:**
- Read `specs/data-classes/index.md` which lists every available data class and its spec file

**Step 2 — Match:**
- For each chunk, determine if it fits one of the listed data classes
- If it fits → proceed to Step 3
- If it does not fit → prompt Daniel:
  - "This content doesn't match any existing data class. Does it fit one of these, or should we create a new one?"
  - If Daniel selects an existing class → proceed to Step 3
  - If Daniel says create a new one → invoke `create-data-class` skill, then proceed to Step 3 with the new class

**Step 3 — Update the chunk manifest:**
- Each chunk is tagged with its data class
- Updated manifest is passed forward to `route-memory`

**Returns:**
- `pass` if ANY of the following are true:
  1. A data class exists and matches the content
  2. User was prompted and selected an existing class
  3. User triggered `create-data-class` and a new valid class was created
- `fail` for anything else — unresolvable content, user didn't respond, class creation failed, etc.

---

**Dependency: `create-data-class` skill**

A separate skill invoked when a new data class is needed on the fly. A data class spec must define (at minimum):
- Name and description
- Which storage bucket (vector store, knowledge graph, or both)
- Required fields / schema
- **Prescribed action** — what to do when a chunk of this class is saved

_(Spec format TBD — to be defined when we design this skill)_

---

### 3. `route-memory`

**Responsibility:** Determine what action each classified chunk requires and where it should be saved. Produces a routing manifest for `save-memory`.

**Step 1 — Read the chunk manifest** left by `classify-memory`

**Step 2 — For each unique class in the manifest**, open its spec at `specs/data-classes/<class-name>.md` and read the prescribed action.

**Step 3 — Write the routing manifest:**
- One entry per chunk: content, class, destination (vector store, knowledge graph, both, or discard)
- Chunks marked discard/transient are resolved here — they do not appear in `save-memory`

**Returns:** `pass` with routing manifest written, or `pass` with all chunks discarded, or `fail`.

---

### 4. `save-memory`

**Responsibility:** Execute the actual write for each chunk in the routing manifest. Handles conflict detection and user interaction before writing.

**Step 1 — Read the routing manifest** left by `route-memory`

**Step 2 — Load specs once:**
- If any chunks are destined for vector store → load `specs/semantic-memory-save.md`
- If any chunks are destined for knowledge graph → load `specs/knowledge-graph-save.md`
- Both loaded once, not once per chunk

**Step 3 — For each chunk in the manifest, apply the appropriate spec:**

**Vector store path** (`specs/semantic-memory-save.md` — TBD):
- Search for semantic similarity before writing
- If conflict found → notify Daniel:
  - "Additional to existing" → write as new entry
  - "Edit/revision of existing" → delete old embeddings, write new
  - "Supersedes existing" → delete old rows entirely
  - "Revision but keep history" → write new, reference old as prior iteration
- If no conflict → write directly

**Knowledge graph path** (`specs/knowledge-graph-save.md` — TBD):
- Fuzzy search for existing entity
- If multiple possible matches → prompt Daniel to disambiguate
- If one clear match → proceed with upsert per class spec
- If no match → create new node

**Step 4 — Confirm all writes completed**

**Returns:** `pass` if all chunks saved (or discarded) successfully, `fail` otherwise.

**Spec dependencies:**
- `specs/semantic-memory-save.md` — conflict resolution logic for vector store
- `specs/knowledge-graph-save.md` — entity disambiguation logic for KG

---

## Spec Files — Summary

| File | Purpose | Status |
|------|---------|--------|
| `specs/data-classes/index.md` | Master list of all data classes | Created — empty, populate as classes are defined |
| `specs/data-classes/<class-name>.md` | Per-class definition + prescribed action | TBD — one per class |
| `specs/semantic-memory-save.md` | Vector store write procedure + conflict resolution | Created |
| `specs/knowledge-graph-save.md` | KG write procedure + entity disambiguation | Created |
| `specs/notify-action.md` | Notify action: one-time, daily scheduled, or unsupported recurring | Created |
| `specs/pin-memory-action.md` | How to write correctly to MEMORY.md | Created |
| `specs/create-data-class.md` | Steps for creating a new data class spec | Created |

**Note:** `specs/create-data-class.md` lives in `specs/` (not `specs/data-classes/`).
It specifies what a valid data class spec must contain, and instructs the agent to
add the new class to `specs/data-classes/index.md` upon creation.

---

## Notes / Open Questions

-

