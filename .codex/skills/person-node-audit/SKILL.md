---
name: person-node-audit
description: Audit Person nodes in the knowledge graph for missing first_name/last_name properties and backfill them. Flags edge cases for manual review.
---

# Person Node Audit

Finds Person nodes that are invisible to `search_person` because they lack `first_name` and/or `last_name` properties, then backfills what can be reliably split and flags the rest for Daniel.

## Step 1 -- Query for incomplete nodes

Call `audit_person_nodes(agent_id="ada")` to get all Person nodes where `first_name IS NULL OR last_name IS NULL`.

If the result has `found: false` -- report "All Person nodes have first_name and last_name set. Nothing to do." and stop.

## Step 2 -- Classify each node

For each node in the results, examine the `name` property and classify into one of two buckets:

### Auto-backfill (simple two-part names)

A name is auto-backfillable when it consists of exactly two whitespace-separated words, neither of which contains titles, suffixes, or other complications. Examples:
- "David Stewart" -> first_name="David", last_name="Stewart"
- "Jane Smith" -> first_name="Jane", last_name="Smith"

### Edge cases (flag for review)

Any name that does NOT meet the auto-backfill criteria. Examples:
- Single name: "Xiaolan"
- Three or more parts: "Mary Jane Watson"
- Contains titles: "Dr. Ruth Elledge", "Coach Johnson"
- Contains suffixes: "Robert Smith Jr.", "William Gates III"
- Hyphenated last names: "Sarah Connor-Reese"
- Non-Western name patterns where first/last order may differ
- Already has one of first_name or last_name set but the other is missing -- check if the existing value is consistent before overwriting

## Step 3 -- Auto-backfill

For each auto-backfillable name, call:

```
update_person_names(name="<full name>", first_name="<first>", last_name="<last>", agent_id="ada")
```

Collect results (successes and failures).

## Step 4 -- Flag edge cases

Present the edge case list to Daniel in a clear format:

```
These Person nodes need manual review:
- "Xiaolan" (single name -- is this first or last?)
- "Dr. Ruth Elledge" (has title prefix)
- ...
```

For each, suggest what first_name and last_name might be, but do not write without Daniel's confirmation.

## Step 5 -- Verify

After all updates, verify a sample of the backfilled nodes by calling `search_person` with the newly set first/last names to confirm they are now discoverable.

## Step 6 -- Report

Summarize:
- N nodes auto-backfilled successfully
- M nodes flagged for manual review (list them)
- Any failures encountered
