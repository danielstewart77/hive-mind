# Training-capture data contract

This document is the authoritative description of the `training_turns` table:
what every row holds, how the captured turns are structured, and the exact
procedures for turning the raw store into training sets for both reasoning
and non-reasoning models. It lives next to the code that writes the table
(`core/training_capture.py`, `core/training_capture_claude.py`,
`core/training_capture_codex.py`) so the rules and the data never drift apart.

If you are about to write an export pipeline, a curation pass, or a
synthetic-reasoning generator, read this file first. Everything you need to
reconstruct a session and place reasoning correctly is specified here.

## What the dataset is for

The dataset teaches an open model to *drive* the Claude Code and Codex CLI
harnesses — tool-call syntax, skill invocations, structural delimiters, and
the policy of when to reach for which tool. Capture is **lossless and raw**:
the row stores what the harness emitted, in order, with real tool names kept
verbatim. Filtering, anonymization, and reasoning-stripping are deferred to
optional export-time passes over this immutable store, never applied at
capture time.

## Grain: one row per turn

A **turn** spans from one human (user) message to the next. One turn becomes
exactly one row. A row carries:

- the user prompt that opened the turn, and
- the assistant's complete ordered response to it — its thinking, its tool
  calls, the results of those tool calls, and its final text — up to the
  next human message.

Tool calls and their results live **inside** the turn they belong to. A
single turn routinely contains many tool calls (grep, read, edit, …), and
the assistant may reason, act, observe results, then reason again before
answering. That interleaving is preserved exactly (see *Assistant block
sequence* below).

Rows reassemble into a full session by ordering on `session_id` then
`turn_index`. The grain is the turn; the training unit at export time may be
a single turn or a whole reassembled session, depending on the recipe.

## Schema

Table `training_turns`, keyed for upsert on `(session_id, turn_index)`:

| column            | type             | meaning |
|-------------------|------------------|---------|
| `id`              | INTEGER PK       | autoincrement surrogate |
| `session_id`      | TEXT NOT NULL    | the harness session this turn belongs to |
| `turn_index`      | INTEGER NOT NULL | 0-based position of the turn within the session; the ordering key |
| `mind_id`         | TEXT             | the mind that produced the turn |
| `harness`         | TEXT NOT NULL    | `claude_code` or `codex` |
| `source_model`    | TEXT             | model the session ended on |
| `harness_version` | TEXT             | CLI version |
| `captured_at`     | INTEGER          | unix seconds at capture |
| `system_prompt`   | TEXT             | session system prompt (denormalized onto each row; same for every row of a session) |
| `user_content`    | TEXT             | the human prompt that opened this turn |
| `assistant_blocks`| TEXT (JSON)      | the assistant's ordered response, as the block array described below |
| `has_reasoning`   | INTEGER (0/1)    | 1 if `assistant_blocks` contains any `thinking` block; the cheap filter that avoids scanning the JSON |
| `tool_call_count` | INTEGER          | number of `tool_use` blocks in this turn |
| `length_tokens`   | INTEGER          | rough size estimate (chars / 4) |
| `quality_flag`    | TEXT             | curation, default `pending`; preserved across re-capture |
| `judge_verdict`   | TEXT             | curation; preserved across re-capture |
| `judge_confidence`| REAL             | curation; preserved across re-capture |
| `exclusion_reason`| TEXT             | curation; preserved across re-capture |

`UNIQUE(session_id, turn_index)`. Indexes on `harness`, `source_model`, and
`has_reasoning`.

`has_reasoning` is intentionally **not** redundant with the JSON. It is the
denormalized index that lets a training-set query select reasoning rows or
non-reasoning rows with plain SQL — `WHERE has_reasoning = 1` — without ever
opening `assistant_blocks`.

## Assistant block sequence

`assistant_blocks` is a JSON array of typed blocks in the exact order the
assistant produced them. Block types:

```json
{"type": "thinking", "text": "..."}
{"type": "text", "text": "..."}
{"type": "tool_use", "name": "<real tool name>", "input": {...}, "id": "..."}
{"type": "tool_result", "content": "...", "tool_call_id": "..."}
```

The ordering of the array is load-bearing. It is the only thing that records
*where* each reasoning blob sits relative to the actions it produced. A
reasoning blob owns the run of actions from its position up to the next
`thinking` block (or to the end of the turn). The action that follows a
reasoning block is the anchor for that reasoning — never a placeholder tag.

There is never an empty `thinking` block. Reasoning is present in its real
position or entirely absent. An empty reasoning shell trains a reasoning
model to open a thought and emit nothing, which is the one actively harmful
representation; we never write it.

## Capture-time transforms

Only these transforms are applied when a transcript is parsed into rows.
Everything else is preserved verbatim.

- **Readable thinking is kept.** Claude's plaintext extended-thinking blocks
  are captured as `thinking` blocks in their real positions.
- **Encrypted reasoning is dropped.** Claude `redacted_thinking` (encrypted)
  and Codex `reasoning` items (encrypted) carry no readable signal and are
  never stored. Dropping them does not distort the transcript: the assistant
  message and its tool calls fall adjacent and read as a clean
  user → assistant → tool flow.
- **Sidechains are skipped** (Claude). Sub-agent transcripts belong to their
  own session; the parent keeps only the sub-agent tool call and its result.
- **Developer / system messages are skipped from the turn body** (Codex).
  They are harness scaffolding; the real system prompt is captured into the
  `system_prompt` column from `session_meta.base_instructions`.

Consequence: Codex rows always have `has_reasoning = 0` today (Codex exposes
no readable reasoning). Claude rows have `has_reasoning = 1` on any turn
where readable thinking survived.

## Idempotency

Each Stop-hook fire re-reads the full transcript and upserts every turn row
by `(session_id, turn_index)`. A transcript only ever grows, so turn rows
are added or overwritten, never deleted. On conflict the capture-time
columns are overwritten with the latest shape while the curation columns
(`quality_flag`, `judge_*`, `exclusion_reason`) are preserved, so a
re-capture never clobbers a verdict.

## Reconstruction and export recipes

The raw store keeps thinking in place; stripping it is an export decision,
so a turn's position is never lost.

**Non-reasoning model.** Render each turn dropping every `thinking` block.
The remaining `text`, `tool_use`, and `tool_result` blocks stay in order and
read as a clean action sequence. No placeholder is left behind.

**Reasoning model.** Render each turn with `thinking` blocks inline, each in
front of the action group it produced, in whatever thinking format the target
model expects. Select the rows with `WHERE has_reasoning = 1` when you want
only turns that carry real reasoning.

**Reassembling a session.** Order rows by `turn_index` within a `session_id`.
Each row contributes its `user_content` followed by its rendered
`assistant_blocks`.

## Adding synthetic reasoning (Codex, or any non-reasoning turn)

Codex turns have no reasoning. To generate it later, the turn row is the
anchor and the `tool_use` blocks are the insertion points — no placeholder
is needed because position is determined by the action that follows.

Procedure for one turn row:

1. Split the `assistant_blocks` array into action groups. An action group is
   a run of `tool_use` / `tool_result` blocks (and any trailing `text`) that
   should share one rationale. The simplest grouping is one group per
   `tool_use` plus one for the final `text`; a coarser grouping (one
   rationale for a parallel batch of calls) is equally valid because the
   schema does not constrain it.
2. For each group, give the generating model the `user_content`, the prior
   results in this turn, and the action(s) in the group, and ask it for the
   reasoning that would have produced that action.
3. Insert the generated text as a `thinking` block immediately **before** the
   group it explains. Repeat for each group, so a turn with three action
   groups receives up to three `thinking` blocks, each in its correct slot.
4. Set `has_reasoning = 1` on the row.

Because every action keeps its ordinal position, the generated reasoning
lands exactly where the original reasoning would have been, and the chunking
choice in step 1 is free to change without altering the storage format.
