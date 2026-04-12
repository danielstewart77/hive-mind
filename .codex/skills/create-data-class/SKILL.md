---
name: create-data-class
description: Creates a new data class spec file in specs/data-classes/ and registers it in the index. Use when classify-memory encounters content that doesn't fit any existing class, or when Daniel requests a new class directly.
---

# Create Data Class

Creates a new data class spec and registers it in the index. Follow in order.

## Step 1 — Name check

Read `specs/data-classes/index.md`. Review all existing class names.
New name must not create ambiguity — no synonyms, no overlap in meaning with existing classes.
If the proposed name is too close to an existing one, flag it and ask Daniel to choose a different name before proceeding.

## Step 2 — Description

Write a description answering:
1. What kind of content belongs in this class?
2. What does it look like — what are the distinguishing features that make it recognizable?

Criteria:
- One to three sentences max.
- Specific enough that classify-memory can match without ambiguity.
- Includes "recognizable by" signals, not just category labels.

Bad: "Information about people."
Good: "A named individual — recognizable by a person's name as the subject, with associated facts about their role, relationship to Daniel, or preferences."

## Step 3 — Actions

Assign one or more actions:

- `discard` — no storage, no action (exclusive — cannot combine)
- `save-vector` — write to semantic vector store
- `save-graph` — write to knowledge graph
- `pin-memory` — append to MEMORY.md
- `notify` — set a reminder or scheduled alert

Any combination of non-discard actions is valid. Choose only what the class genuinely requires.

## Step 4 — Write the spec file

Create `specs/data-classes/<class-name>.md`:

```
# Data Class: <name>

## Description
<from Step 2>

## Actions
<from Step 3>

## Notes
<edge cases, examples, guidance for classify-memory>
```

## Step 5 — Update the index

Add a row to `specs/data-classes/index.md`: Class Name | Spec File | Storage Bucket | one-line Description.

## Format note

Spec files are read by Ada, not humans. Write minimally. No padding, no preamble. Every line must carry information.
