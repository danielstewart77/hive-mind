---
name: backlog
description: |
  Read, create and order work on the hive Kanban board — epics, features and stories. Trigger when Daniel asks what is next, what a feature contains, or in what order its stories are worked; when work is to be added to the backlog; and whenever a story's lane has to move as work starts or finishes.
harness:
  claude:
    user-invocable: true
    model: inherit
---

# /backlog

The board is the only backlog: the `work_items` table of the hive's one
database, served by the `hive-site` console. Reach it through `board`, which
is the console's own board routes — never raw SQL against `work_items`, and
never `docker exec`. The ordering, the hierarchy numbering and the write
lock live in the console's board module above that table, and a statement
run under it skips all three.

`board` below is
`/opt/venv/bin/python3 "$HIVE_PROJECT_DIR/tools/stateless/board/board.py"`.
It prints one JSON object and exits 1 with `{"error": ...}` when refused.

    board list [--kind epic|feature|story] [--status S] [--parent ID]
    board show <id>                     one row, with its backlog number
    board relations <id>                its parent, children and choices
    board create epic --title T [--description D]
    board create feature --title T --epic <id> [--description D]
    board create story --title T (--feature <id> | --epic <id>)
                       [--description D] [--before <id>] [--status S]
    board update <id> [--title T] [--description D] [--status S]
    board move <id> --before <id>

Statuses are `backlog`, `committed`, `in_progress`, `done`. `list` comes
back in backlog order, each row carrying the number the operator reads
(`2.17.6`).

There is no delete, and none to reach: finished work goes to `done` and
stays on the board. Abandoned or blocked work goes back to `committed` with
the reason added to its description.

## What each kind carries

**Epic** — the overall effort and why it is being done. No sequence; its
features are the sequence.

**Feature** — what the feature is, and **its stories in order**. The order
lives in two places on purpose and they must agree: position is the order
itself, which is what the board renders and what the number `2.17.6` comes
from; the feature's description carries the same sequence written out, one
line per story, with what each contributes and what blocks what. Position is
the authority on order, the description is the authority on why. A feature
whose description does not name its stories in order is incomplete and gets
fixed before any of its stories is worked.

**Story** — one instruction, complete enough to implement from. Never an
open question: ask Daniel, then write the decided story. A decision recorded
in a story is labelled `DECIDED` with its date and who made it.

## Answering "what's next"

1. Find the feature in flight — the one whose stories are moving.
2. Read its description's story order.
3. The answer is the first story in that order that is not `done`, and the
   reason is whatever the order says blocks it.

Never pick the next story by searching the board for a topic. That is how a
side errand gets offered in place of the work everything else waits on. When
several features are open, say which feature the answer comes from.

## Adding work

A story is created and placed in one operation — `create story` under its
feature with `--before` for its point in the sequence — and its line is
added to the feature's description. A story created without that line is
invisible to the question above.

## Moving work

A story's lane is how Daniel follows it: `committed` once its requirements
are signed off, `in_progress` when implementation starts, `done` only once
merged and pulled. You move a lane because work actually started or
finished, never to tidy the board.

## When it refuses

- *refused this mind's board credential* — `BOARD_PUSH_TOKEN` is unset or
  is not the value the console holds. Not yours to rotate: tell Daniel.
- *does not reach ...* — that operation is not on this token, by design.
  Say so plainly rather than looking for another way round it.
- *did not answer* — the console is down. Report it; the board is not
  readable from anywhere else.
