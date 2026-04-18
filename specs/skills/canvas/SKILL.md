---
name: canvas
description: Write content to Ada's live Spark to Bloom canvas. Use when you need to display a document, plan, or structured content on the canvas at sparktobloom.com/canvas.
user-invocable: true
---

# Canvas

Ada's live workspace on the Spark to Bloom website. Writing to the canvas file makes it immediately visible at `sparktobloom.com/canvas` via a bind mount — no restart needed.

## Canvas File Location

```
/home/hivemind/dev/spark_to_bloom/src/templates/canvas.md
```

This file is bind-mounted from the host at `${HOST_SPARK_DIR}/src/templates/canvas.md`. Changes are live instantly.

## How to Write to the Canvas

**Always read the file first**, then use the Edit or Write tool to update it.

1. Read `/home/hivemind/dev/spark_to_bloom/src/templates/canvas.md`
2. Replace the content with the new document (full overwrite) or edit a specific section
3. The change is immediately live at `sparktobloom.com/canvas`

## Format

The canvas renders Markdown. Use standard Markdown: headings, tables, code blocks, bold/italic. Inline HTML is also supported for styled elements.

## Conventions

- Lead with a `# Title` heading
- Add a `> **Status:** ...` blockquote under the title for context
- Use `---` horizontal rules to separate major sections
- Keep it focused — the canvas is a single working document, not a multi-page site

## When to Use

- Displaying a plan or spec for Daniel to review
- Putting a design document in front of both parties during a discussion
- Sharing structured output that benefits from formatted rendering
