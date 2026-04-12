---
name: knowledge-graph-save
description: Procedure for writing a memory chunk to the knowledge graph. Use when save-memory is processing a chunk routed to save-graph. Handles fuzzy entity search, disambiguation, and user resolution before writing.
---

# Knowledge Graph Save

Governs how save-memory writes chunks to the knowledge graph. Load once per run for all save-graph chunks.

## Step 1 — Fuzzy Entity Search

**For Person entities:** use `search_person`. Pass whatever name fragments are known as named params — `first_name`, `last_name`, `title`, `relationship`. All use case-insensitive substring matching. Pass only what you know; omit the rest. Always include `agent_id` (e.g. `agent_id="ada"`).

Examples:
- Know their first name → `search_person(first_name="Manny", agent_id="ada")`
- Know their title → `search_person(title="Coach", agent_id="ada")`
- Know their relationship to Daniel → `search_person(relationship="wife", agent_id="ada")`
- Know first + last → `search_person(first_name="Wil", last_name="Vark", agent_id="ada")`
- Natural language like "my kids" → try `search_person(relationship="child", agent_id="ada")`, then `relationship="children"` if no results

Results are returned as a `matches` array.

**For all other entity types** (Project, System, Concept, etc.): use `graph_query` with the entity name or fragment. Always include `agent_id`.

## Step 2 — Evaluate Results

No matches → proceed to write. Every node must have at least one edge — if no relationship can be established from the chunk, surface this to Daniel before creating an isolated node.

One clear match → upsert using `graph_upsert`. Update properties per the data class spec.

Multiple possible matches → do not write. Surface to Daniel (Step 3).

## Step 3 — Prompt Daniel (multiple matches only)

Present the incoming entity and candidate matches. Suggest the most likely match, let Daniel decide:

- Matches one of these → upsert into selected node.
- New distinct entity → create new node.
- Two existing nodes should be merged → merge, then upsert.
- Add as relationship only → create edge, no new node.

## Step 4 — Write

Call `graph_upsert` with entity type, name, properties, and relationship fields per data class spec and Daniel's decision.

## Notes

- Isolated nodes (no edges) are a red flag — always flag before creating.
- Fuzzy search is intentional: typos and aliases are common in conversational input.
- When merging nodes, preserve all existing relationships from both before deleting one.
