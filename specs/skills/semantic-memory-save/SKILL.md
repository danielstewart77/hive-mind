---
name: semantic-memory-save
description: Procedure for writing a memory chunk to the vector store. Use when save-memory is processing a chunk routed to save-vector. Handles similarity search, conflict detection, and user resolution before writing.
user-invocable: false
---

# Semantic Memory Save

Governs how save-memory writes chunks to the vector store. Load once per run for all save-vector chunks.

## Step 1 — Similarity Search

Run semantic similarity search using the chunk content as the query. Threshold: cosine > 0.85.

## Step 2 — Evaluate Results

No similar entries found → write directly, done.

Similar entries found → assess conflict:
- Content is clearly distinct despite surface similarity → write directly.
- Content covers the same topic or fact → surface to Daniel (Step 3).

## Step 3 — Prompt Daniel

Present the incoming chunk and conflicting entries. Suggest the most appropriate option based on content — do not force a fixed menu:

1. Supersedes old data → delete conflicting rows, write new chunk.
2. Update, keep history → write new chunk with reference field linking to prior iteration(s). Both remain.
3. Additional context, both valid → write new chunk alongside existing. No deletion.

If Daniel says something outside these options, interpret intent and confirm before acting.

## Step 4 — Write

Call `memory_store` with chunk content, data class, tags, and any reference fields from Step 3.

## Notes

- Never silently overwrite. Always surface conflicts before deleting.
- "Keep history" is the safest default when uncertain — suggest it first.
- Reference fields for chained history go in the memory entry's metadata.
