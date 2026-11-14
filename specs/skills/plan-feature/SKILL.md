---
name: plan-feature
description: Turn a feature Daniel wants into a planned feature on the hive board — requirements interrogated against the real codebase, split into stories ordered for parallel work, each story written by its own delegate. Trigger when Daniel asks for a feature to be planned, broken into stories, or added to the backlog as more than a single piece of work; and before any multi-story build begins.
harness:
  claude:
    user-invocable: true
  codex:
    user-invocable: true
  dsh:
    user-invocable: true
---

# /plan-feature

Planning ends with rows on the board, not with a document. `board` below is
`/opt/venv/bin/python3 "$HIVE_PROJECT_DIR/tools/stateless/board/board.py"`;
`/backlog` carries its full surface. A feature needs an epic to hang from —
if Daniel has not named one, ask which, and ask nothing else in that turn.

## 1. Collect the requirements

Interrogate until a requirement list is complete, one question per turn.
Ask what the feature is for and who uses it, what it must do, what it must
refuse, and what tells Daniel it works. Stop when the next question would
only confirm something already answered.

## 2. Read the codebase it lands in

Before writing anything down, find where this feature actually lives: the
modules it changes, the conventions those modules keep, the tests that cover
them, the design document that governs them. A plan written without this
names stories that cannot be built in the order given.

## 3. Ask what the codebase just raised

Reading it always raises questions the first pass could not: an existing
mechanism that already does half of this, a constraint that makes a stated
requirement impossible, a caller nobody mentioned. Put those to Daniel and
fold the answers into the requirement list. Skip this step only when the
reading raised nothing, and say so.

## 4. Split it into stories

A story is one behaviour, buildable and verifiable on its own. Split on
behaviour rather than on layer: a story that ships a schema with no caller
cannot be verified and is not a story.

Look deliberately for work that can run at once. Two stories are parallel
when neither reads what the other writes — not merely when they touch
different files. Say which are parallel and why, because the order is what
decides whether this feature takes a day or a week.

## 5. Diagram the feature's logic

If the feature expresses complex logic — more than one condition, decision
or action — add a mermaid diagram of that logic to the feature's
description. A linear feature gets no diagram; a diagram of a straight line
is noise.

## 6. Diagram the build order

Always. One mermaid diagram of the stories, showing what runs in parallel
and what must wait on what. Parallel stories share a rank; a dependency is
an edge, and every edge carries the reason it exists.

## 7. Hand each story to a delegate

Create the feature on the board first (`board create feature --title T
--epic <id> --description D`), with both diagrams in its description. Then
give one delegate per story the brief in `briefs/write-a-story.md`, filled
in with the feature id, the story's behaviour, its place in the order, and
what the codebase reading found about where it lands. Run them at once —
that is the point of having split the work.

When they return, read what they wrote: a delegate that misunderstood a
story writes a plausible row, and the board is what gets built from.

Report the feature's backlog number, its stories in build order, and which
of them can start immediately.
