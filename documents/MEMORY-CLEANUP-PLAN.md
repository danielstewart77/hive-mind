# Memory Cleanup Session

## Mission
Go through every entry in the Neo4j vector store and clean it up.

## Tools Available (new this session)
- `memory_list(offset, limit)` — paginate through all Memory nodes sequentially by creation time
- `memory_delete(memory_id)` — delete a node by its element ID

## Process

1. Call `memory_list(offset=0, limit=25)` to get first batch
2. For each entry, decide:
   - **Obvious garbage** (e.g., "Done.", "topic1", malformed epilogue fragments, single words, test artifacts) → delete immediately, no discussion
   - **Valid content, no data class** → check `specs/data-classes/index.md` — if a class fits, assign it. If not, use `create-data-class` skill to create one, then classify.
   - **Questionable** → present to Daniel and discuss before acting
3. After processing a batch, call `memory_list(offset=25, limit=25)` for next batch, etc.
4. Continue until all entries reviewed (currently ~192 Memory nodes)

## Rules
- Be aggressive on obvious garbage — don't ask about "Done." or "topic1"
- Be conservative on anything with real content — when in doubt, discuss
- Note: total count may shrink as you delete, so track by offset carefully (offset stays fixed, total count drops)

## Current State (as of 2026-03-06 session)
- 373 total graph entries (192 Memory nodes, 140 graph entity nodes, 37 classified as technical-config, 4 entity nodes classified)
- Most Memory nodes are unclassified session epilogue garbage
- Known good entries that should be kept/classified: architecture notes, tool builds, bug fixes, session learnings with real content

## Also on agenda this session (if time)
- Create Planka card for person data class update (add `first_name`, `last_name`, `title` fields)
- Update Coach Manny's graph entry to add `first_name: Manny`, `title: Coach` (last name still unknown)
