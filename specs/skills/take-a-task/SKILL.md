---
name: take-a-task
description: Take a piece of work Daniel hands over, give it to the mind that should do it, and open its ledger row. Trigger when Daniel asks for something to be done, looked into, built or answered, or points at an existing session and asks you to watch it.
---

# /take-a-task

`ledger` below is `/opt/venv/bin/python3 "$HIVE_PROJECT_DIR/tools/stateless/staff_ledger/staff_ledger.py"`;
it prints JSON and exits nonzero with `{"error"}` on a refusal. Every comms
call goes to `$COMMS_URL` with
`-H "Authorization: Bearer $COMMS_BEARER_TOKEN" -H "Content-Type: application/json"`:
your own credential, which the gateway accepts for a mind of your owner.
Only the live feed, `GET /sessions/live` and `GET /sessions/live/text`, needs
the admin token, `$COMMS_ADMIN_BEARER_TOKEN`.
Never the broker: it ends a callee's session after each wake, which orphans
the task.

## What this skill needs from the install

It is written for a chief of staff running in a hive container, and it
works only where these are true. None of them arrives with the skill:

- `/opt/venv/bin/python3` is the container's interpreter and
  `$HIVE_PROJECT_DIR` the hive-mind checkout, with `hive_data` importable
  (the `mind-ledger` image target) and `HIVE_DATABASE_URL` naming a login
  that reaches the `staff` schema.
- `$COMMS_URL`, `$COMMS_BEARER_TOKEN` (your own credential) and
  `$HIVE_SESSION_ID` (this conversation) are in the environment.
- `$MIND_NAME` is this mind's folder name under `minds/`.
- `board` is the board tool as `/backlog` describes it, with its token.
- The gateway wakes you when a session you briefed stops. There is no watch
  container and no timer beside you.

## You orchestrate; sub-agents execute

Every step below that talks to something outside this conversation is handed
to a background sub-agent, and you do only what is sub-second and has to be
true before you speak. Daniel's words for why: "your text just cuts off and
it waits until the other agents that you're managing to finish before I get
any text back from you, even when that text from you is not blocked by those
agents."

A brief lives in `<skills directory>/take-a-task/agents/`. You never retype
one — you start a background sub-agent, with whatever your harness delegates
by (`subagent` on dsh), and a prompt that names the file by its full path and
fills in its parameters:

    description: "judge session 1a2b"
    prompt: "Read <skills directory>/take-a-task/agents/read-and-judge.md and follow it exactly. Session: <id>. Task: <task id>. Task description: <text>. Board item: <number or none>."

`<skills directory>` is where your harness loads this skill from: the
directory holding this file's own `take-a-task` folder.

They run in the background and settle on their own; several at once is normal
and your harness's listing of its children (`list_agents` on dsh) shows them. Then **say what went out, in that same turn**,
naming each job and the mind it went to, and end the turn. Do not wait for a
child to finish before speaking. Report each result when it lands.

**Call them sub-minds.** In everything said to Daniel, a background child is a
"sub-minds", never a "helper" and never a "sub-agent". The tool keeps whatever
name your harness gives it; only the word you speak changes. (Daniel, 2026-11-14.)

**One report per child.** A child's final message is delivered to you twice,
once as its report and once as its closing note, so a child that writes a
result and then a summary says everything twice. End every sub-agent prompt
with: "Give one final message only. Put the whole result in it. Do not add a
separate summary, recap or closing note after it." (Daniel, 2026-11-14.)

A child is in this container with your environment, so it reaches comms and
runs `ledger` itself. Every status move carries `--expect`, so several
children writing at once is safe. What is never a child's: deciding a status,
talking to Daniel, and editing the roster or a skill. Those are yours.

1. **Classify it.**
   - *Simple* — one turn. Daniel's words are the task.
   - *Complex* — several turns. It may need a story on the board.
   - *Very complex* — several turns and stories. It is a feature.
2. **Pick the mind.** The one Daniel named; otherwise the best fit in
   `<skills directory>/take-a-task/roster.md`. Its id is the `id` of its row
   in `GET /broker/minds`. Weigh subscription pressure when choosing between
   a Claude mind and a codex mind: `/opt/venv/bin/python3 "$HIVE_PROJECT_DIR/tools/stateless/provider_pressure/provider_pressure.py"`
   (read-only, sub-second, same figures as the dashboard). `used_percent` is
   how much of that provider's 5h or 7d allowance is spent; `band` is `ok`
   under 60, `warn` 60 to 85 (approaching the limit), `hot` over 85. Read
   `worst` and `approaching`; a provider in `unknown` has no current reading,
   which is not headroom. Ignore a window with `reset_passed: true` — it has
   already reset — and treat `stale: true` as a floor, not the current figure.
   Exit 1 with `{"error","kind"}` means no reading, not no pressure.
3. **Arrange the board item when needed.** Dispatch a background helper to
   record the story or feature on the Kanban board before briefing the mind;
   confirm it landed and associate its id with the ledger row.
3a. **Fit it into something that already exists (Daniel, 2026-11-17).**
   Before anything is opened, look at what is already there: `ledger list`
   for the tasks and `board list` for the features and stories. A request
   that is close to something already on the board or the ledger goes under
   it, as a new story under that feature or a task under that story, and
   not out on its own as a new task. Two or three very similar things
   standing as separate tasks is the failure this prevents. When it is
   close and you are not certain, ask him, naming the item: "should this go
   as a new story under <feature>, or as a task under <story>?" Never
   settle the doubt by defaulting to a new task. Only work that is plainly
   unlike anything open stands alone.
3a-i. **A new session is for a brand new task only.** Before opening one, run
   `ledger list` and ask whether the work belongs to a task that is already
   open: the same story, the same feature, a follow-up or a question about
   something a mind is still working on. If it does, send it into that
   task's existing session with `brief-a-session.md` and open no row and no
   session. Open a new session only when the work is unrelated to anything
   open. When it is unclear which, ask Daniel rather than guessing. (Daniel,
   2026-11-14.)
3b. **Every task Daniel hands over goes on the Kanban board as a task
   (Daniel, 2026-11-15).** Whenever he tells you to take something on, the
   board item is created automatically, without asking, as a task, standing
   alone only when step 3a found nothing it belongs under, and its id goes
   on the ledger row
   with `ledger edit <task> --board-item <id>`. The board has no task type
   yet: it is part of the Kanban redesign being planned, so until that
   ships, say once that the board cannot hold it and do not invent a story
   to stand in for it. Once the type exists, this step is not optional.
4. **Open the session.** `POST /sessions` with
   `{"surface": "web", "surface_id": "terminal", "client_ref": "staff-<uuid>", "mind_id": "<id>"}`,
   a fresh uuid each time (`cat /proc/sys/kernel/random/uuid`). Anything but
   a 200 is a failure to report to Daniel; the session is `jq -er .id` of
   the answer. `web` is what lets Daniel pick it up from any surface later.
5. **Open the row** before briefing, so a session that stops at once is
   already watched:
   `ledger open --name "<short name>" --session <id> --description "<the task>"`,
   then `ledger edit <task> --board-item <id>` if the work already has one.
   The description is Daniel's ask in his words. It also carries where the
   task stands whenever that is not obvious from the session — the board
   item it covers once there is one, and for a feature which stage it is
   at — kept current with `ledger edit <task> --description`.
6. **Name the session before briefing.** Give every session you create a name
   in the form `CoS: <task name>`, with a one-to-three-word description after
   the prefix. Put the name in the `POST /sessions` body of step 4 as `name`,
   with your admin bearer in the ordinary `Authorization` header and no
   `X-Rename-Token` (a wrong one beside the admin token is refused). Read
   `.name` back off that same answer: names are trimmed and cut to forty
   characters, and a null means the gateway predates the change, so fall back
   to `PUT /sessions/{id}/name` and verify it with a GET. Report a refusal
   rather than pretending the session is named. (hive-mind PR 379, 2026-11-14.)
7. **Dispatch the brief**, and say so. Hand `brief-a-session.md` a
   sub-agent with the session, the task id, the mind and Daniel's words, then
   tell Daniel it has gone out and end the turn. The child sends it detached
   and confirms it landed; a brief that never arrived comes back from the
   child and you tell him then.
   - **Landed means accepted.** The brief asks the gateway to wake you, and
     the gateway answers `202` with `accepted: true` once it holds the watch
     and has taken the brief. Nothing is held open and nothing is saved to a
     file. Report nothing as sent, and leave no row reading as dispatched,
     until you have seen that `202`. The wake comes back to *this*
     conversation: a brief sent from a conversation that is then ended is
     never answered, so after `/new` or `/clear` ask again for every open row
     (see `/tend-the-ledger`).
   - *Complex*: if it needs more than one turn, put it on the board as a
     story first and name it in what you tell him.
   - *Very complex*: create the feature on the board, with no stories yet,
     and name its number. The description says the feature awaits your check.
     When that turn stops, hand `check-a-feature.md` a sub-agent with the
     number and Daniel's words; if it comes back wrong, tell the same agent
     what to fix; once it is right, tell it to create the stories and begin,
     and record the feature in the description.

## Finished means in use, and a brief says so

Daniel's rule (2026-11-17): when functionality is good and confirmed, it is
merged, rebuilt, restarted or recreated, whatever the container or the target
needs for the work to be complete and in use. Every brief that assigns work,
and every relay of his instructions into a session, carries that as what done
means. Never write "restart nothing" or "recreate nothing" into a brief as a
limit of your own: on 17 November two briefs to the operator mind carried it, Daniel
heard it as his command, and it was neither his nor right. It also went
against his 15 November preference in the roster.

The one caveat is his as well: the mind waits until the system is quiet.
Nothing is rebooted, restarted or recreated while something is running that
it would cut off; the mind looks for an idle moment and does it then. Give
the mind the facts it needs to judge quiet, such as which sessions are live
and which conversation Daniel is in, rather than a prohibition. Only when no
idle moment can be found does it come back to Daniel, as a question on the
row.

What gates a merge is confidence, not caution about restarts: the mind must
be quite sure the merge is clean and disrupts nothing in flight, for itself
or for any other mind. When it cannot be sure, that is the one thing that
comes back to him.

## Every child registers itself, so the page can draw it

A sub-agent lives inside the turn that spawned it and is invisible from
anywhere else, so the Chief of Staff page can only draw one that wrote its
own row. Every brief begins and ends with the same two lines, and the brief
files say so themselves:

    ledger agent-start <task> <brief file name> --about "<the one thing>"
    ledger agent-done <agent id> --outcome "<finished|failed|waiting|...>"

`--about` is what tells two children on one brief apart on the page - the
session, the board number, the mind. A child that dies with the turn never
reaches the second line; the page reads an unclosed row older than your turn
timeout as orphaned rather than working, so there is nothing for you to
clean up and nothing to pretend about.

## The board item and the question are columns, not prose

Two things the page draws that a description cannot carry:

- `ledger edit <task> --board-item <id>` the moment the task has a board
  item. The page expands a task that names a feature into its stories and an
  epic into features and their stories; with no id it is a single node
  however large the work is. The id, not the number: `f0123456789abcdef`,
  which the board gives you when you create or find the item.
- `ledger edit <task> --question "<the one thing>"` whenever you take
  something to Daniel, and `ledger edit <task> --answered` the moment he
  answers. A task holding a question draws a bubble that rises to you, and
  the count of bubbles is the number of answers the hive is waiting on. The
  status alone cannot say which question, and a bubble that outlives its
  answer is worse than none.

**Every wait on Daniel is a bubble, with no exception (Daniel, 2026-11-15).**
Anything you or a feature is waiting on him for, whether a decision, a
go-ahead, a moment to restart something, a sign-off on a read-back or a
choice between answers, is a question on a row the same turn you put it to
him, so the page draws the bubble. It holds however the question reached him:
from a stopped session, from your own judgement in conversation, or from a
planning read-back. A question with no row to carry it gets a row first:
open one, or attach the work to the task that covers it, then
`ledger edit <task> --question`. Never ask him something in chat that the
page cannot show, and clear it with `--answered` the moment he answers.

## When a sub-agent dies, hangs or is orphaned

The row is the truth and a dispatch is not a result. Say plainly what you do
not know.

- **Died with the turn.** A child lives inside the process running your turn,
  so a conversation that ends, rotates or has its container restarted takes
  every unsettled child with it. Nothing is lost that the row did not hold:
  on the next turn, re-read the row and dispatch again. Never report work a
  dead child was doing as done.
- **Hung.** A turn is stopped at `turn_timeout_seconds` (in your `runtime.yaml`).
  Anything still running at that point reports nothing, so a task whose child
  never settled stays exactly as its row says — `in progress`, unanswered —
  and is dispatched again rather than assumed.
- **Orphaned.** Your harness names the children still live in this turn and
  can read or stop a background job by id (`list_agents`, `job_output` and
  `job_kill` on dsh). A job id
  with nothing to attribute it to is killed, not guessed at.
- **Came back empty or refused.** That is a result and it is reported as one:
  which brief, which session, and what it said. A child that could not read a
  session is not a session with nothing in it.

Never tell Daniel a job finished because you dispatched it. "Dispatched to
the mind" and "the mind says it is green" are different sentences and he is owed
whichever is true.

**Adopting a session Daniel points at** is the same, minus step 3. If
`ledger find <id>` names a `done` task, `ledger detach` it from that task
first.
