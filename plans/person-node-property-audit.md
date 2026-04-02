# Person Node Property Audit — Backfill first_name / last_name

## User Requirements

`search_person` searches the `first_name` and `last_name` properties on Person nodes.
Any node stored without these separate fields (e.g. only `name: "Full Name"`) is invisible
to `search_person`, even when the person exists in the graph with full relationship data.
This was discovered on 2026-04-01 when David Stewart (with wife, 5 kids, email, birthday)
could not be found by `search_person(first_name="David")`.

## User Acceptance Criteria

- [ ] All Person nodes in the graph have `first_name` and `last_name` as explicit properties
- [ ] `search_person(first_name="David")` returns David Stewart
- [ ] `search_person(first_name="Amber")` returns Amber Stewart
- [ ] Running `search_person` on any known person by first name returns a result
- [ ] No Person node has only a `name` field without the split properties

## Technical Specification

### Approach

1. Query all Person nodes from Neo4j
2. For each node, check if `first_name` and `last_name` are present
3. For nodes missing them, parse `name` field (split on first space — first word = first_name,
   remainder = last_name) and backfill via `graph_upsert_direct`
4. Flag any nodes where the name cannot be reliably split (single-word names, etc.) for
   manual review

### Edge Cases

- Single-word names (e.g. "Skippy") — set `first_name` only, leave `last_name` empty
- Names with prefixes/suffixes — leave for manual review
- Nodes already having both fields — skip

## Files to Touch

| File | Change |
|------|--------|
| One-time script or skill | Query all Person nodes, identify gaps, backfill |

## Implementation Order

1. Write a Cypher query (or Python via Neo4j driver) to list all Person nodes missing
   `first_name` or `last_name`
2. Review the list — flag anything that can't be auto-split
3. Backfill auto-splittable names via `graph_upsert_direct`
4. Manually fix flagged nodes
5. Verify `search_person` returns expected results for 3-5 known people
