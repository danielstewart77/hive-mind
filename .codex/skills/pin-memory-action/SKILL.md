---
name: pin-memory-action
description: Procedure for writing a memory chunk to MEMORY.md. Use when save-memory encounters a chunk with the pin-memory action. The data class has already decided it belongs in MEMORY.md — this skill covers how to write it correctly.
---

# Pin Memory Action

The data class determined this content belongs in MEMORY.md. Follow these steps to write it correctly.

## Step 1 — Read MEMORY.md

Read the full contents before writing. Look for existing content covering the same topic or fact.

## Step 2 — Check for duplicates

Same fact verbatim → skip, no write needed.
Related content in same section → update in place, do not append a duplicate.
No related content → proceed to Step 3.

## Step 3 — Determine placement

Find the most appropriate existing section. If none fits, create a new section with a clear heading.
Session-specific notes → "Session Notes — [date]" section only, not permanent sections.

## Step 4 — Write

- Concise — MEMORY.md truncates after 200 lines. Every line must earn its place.
- Short declarative statement of durable fact.
- Match existing style: plain sentences, no fluff, no hedging.
- Do not repeat context already captured elsewhere in the file.

## What does NOT belong in MEMORY.md

- Session-specific context not relevant in future conversations.
- Anything already in vector store or knowledge graph that doesn't need immediate context on every conversation.
- Speculative or unverified facts.
