---
If `mind_id` is not in the `minds` block, stop: "Unknown mind: <mind_id>."
---

## Step 2 — Read the soul file verbatim

Read the entire file at the resolved path.

**Do not summarize. Do not abbreviate. Do not paraphrase. Do not shorten.**
Every sentence is the mind's identity. Every word goes in.

---

## Step 3 — Check for existing graph node

Call `graph_query('<MindName>', agent_id='<mind_id>')` where `<MindName>` is the
capitalized mind name (e.g. `ada` -> `Ada`, `bob` -> `Bob`).

If a node already exists with `soul_values` populated:
- Display the existing content to the user
- Ask: "A graph node for <MindName> already exists. Overwrite? (y/n)"
- If no: stop

---

## Step 4 — Write the graph node

Call `graph_upsert_direct` with:

- Node type: `Mind`
- Node name: `<MindName>`
- Properties:
  - `mind_id`: the mind's ID string (e.g. `"ada"`)
  - `soul_values`: **every non-empty line of the soul file, verbatim, as a list**. No collapsing. No summarizing. One entry per line.
  - `soul_file`: the path to the soul file (audit trail only -- no code reads this)

---

## Step 5 — Verify

Call `graph_query('<MindName>', agent_id='<mind_id>')` again. Confirm the node
was written. Display the stored `soul_values` back so the operator can verify
nothing was lost.

---

## Step 6 — Report

Output:
- Mind: `<mind_id>`
- Soul file seeded from: `<path>`
- Lines stored in graph: `<count>`
- Status: "Sessions will load this mind's identity exclusively from the graph node."

---

## Rules

- Never abbreviate or paraphrase soul content. Every line goes in verbatim.
- This skill writes to the graph. It does not modify the soul file.
- The soul file is an archived seed. Sessions never read it.
- If the graph is unavailable, report the error and stop. No fallback.
