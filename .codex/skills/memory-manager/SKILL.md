---
name: memory-manager
description: Orchestrates the full memory storage lifecycle. Triggers on "remember this" (manual) or new session start (automated). Runs parse-memory → classify-memory → route-memory → save-memory in sequence. Use when memory needs to be stored from a conversation or transcript.
---

# Memory Manager

Orchestrates memory storage. Runs four sub-agents in sequence. Each must pass before the next runs.

## Triggers

- Manual: user says "remember this" (single item or thread)
- Automated: new session starts (full transcript)

## Setup

Create a session temp directory: `/tmp/memory-{timestamp}/`
All sub-agents write their manifests here. Pass the path forward to each agent.

## Step 1 — parse-memory

Invoke the `parse-memory` agent with:
- Trigger type: `manual` or `automated`
- If manual: the content or thread to remember
- If automated: the full session transcript
- Session temp path

On `RESULT: PASS` → proceed to Step 2.
On `RESULT: FAIL` → stop. Report: "Memory storage failed at parse step: {reason}"

## Step 2 — classify-memory

Invoke the `classify-memory` agent with:
- Session temp path (reads chunk manifest written by parse-memory)

On `RESULT: PASS` → proceed to Step 3.
On `RESULT: FAIL` → stop. Report: "Memory storage failed at classify step: {reason}"

## Step 3 — route-memory

Invoke the `route-memory` agent with:
- Session temp path (reads classified manifest from classify-memory)

On `RESULT: PASS` → proceed to Step 4.
On `RESULT: PASS | ALL_DISCARDED` → stop. Report: "Nothing to save — all chunks were classified as transient."
On `RESULT: FAIL` → stop. Report: "Memory storage failed at route step: {reason}"

## Step 4 — save-memory

Invoke the `save-memory` agent with:
- Session temp path (reads routing manifest from route-memory)

On `RESULT: PASS` → report: "Memory stored successfully."
On `RESULT: FAIL` → report: "Memory storage failed at save step: {reason}"

## Cleanup

Delete the session temp directory after Step 4 completes (pass or fail).
