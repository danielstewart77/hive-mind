Stage document for the chief of staff role; it is the text of the skills below with this install's specifics removed.

## take-a-task/SKILL.md

```
---
name: take-a-task
description: Take a piece of work the owner hands over, give it to the mind that should do it, and open its ledger row. Trigger when the owner asks for something to be done, looked into, built or answered, or points at an existing session and asks you to watch it.
---

# /take-a-task

`ledger` below is the ledger tool; it prints JSON and exits nonzero with
`{"error"}` on a refusal. Every comms call goes to `$COMMS_URL` with
`-H "Authorization: Bearer $COMMS_BEARER_TOKEN" -H "Content-Type: application/json"`:
your own credential, which the gateway accepts for a mind of your owner.
Only the live feed, `GET /sessions/live` and `GET /sessions/live/text`, needs
the admin token, `$COMMS_ADMIN_BEARER_TOKEN`.
Never the broker: it ends a callee's session after each wake, which orphans
the task.

## You orchestrate; sub-agents execute

Every step below that talks to something outside this conversation is handed
to a background sub-agent, and you do only what is sub-second and has to be
true before you speak. The owner's words for why: "your text just cuts off and
it waits until the other agents that you're managing to finish before I get
any text back from you, even when that text from you is not blocked by those
agents."

A brief lives in `<skills directory>/take-a-task/agents/`. You never retype
one — you call `subagent` with a prompt that names the file and fills in its
parameters:

    subagent(description: "judge session 1a2b",
             prompt: "Read <skills directory>/take-a-task/agents/read-and-judge.md and follow it exactly. Session: <id>. Task: <task id>. Task description: <text>. Board item: <number or none>.")

They run in the background and settle on their own; several at once is normal
and `list_agents` shows them. Then **say what went out, in that same turn**,
naming each job and the mind it went to, and end the turn. Do not wait for a
child to finish before speaking. Report each result when it lands.

**Call them sub-minds** (top document, rule 10). The tool is still named
`subagent`; only the word you speak changes.

**One report per child.** A child's final message is delivered to you twice,
once as its report and once as its closing note, so a child that writes a
result and then a summary says everything twice (top document, rule 8).

A child is in this container with your environment, so it reaches comms and
runs `ledger` itself. Every status move carries `--expect`, so several
children writing at once is safe. What is never a child's: deciding a status,
talking to the owner, and editing the install's file or a skill. Those are yours.

1. **Send it to the planner first.** You do not judge
   whether a task is simple. Every task the owner hands over to be built,
   changed, fixed or set up goes to the planner mind, and the planner always
   answers with a plan; a simple task comes back as a plan of one step with
   one mind named. The whole of it is the section "Plan, approve, assign in
   waves, review" below, and steps 2 to 7 are how each hop in it is sent.
   Two things skip the planner because nothing is being decided: the owner
   naming the mind himself ("ask <mind>…", "tell <mind>…"), and your own
   housekeeping (a sitrep, a chase, closing a finished session).
2. **Pick the mind.** The one the owner named or the one the approved plan
   names for that step; otherwise the best fit among the minds that hold the
   roles. Who holds a role, and the models, plans, standing orders and routing
   preferences, are read where the top document says, before routing. Its id
   is the `id` of its row in `GET /broker/minds`. Weigh subscription pressure
   when choosing between minds on different providers: the provider-pressure
   tool
   (read-only, sub-second, same figures as the dashboard). `used_percent` is
   how much of that provider's 5h or 7d allowance is spent; `band` is `ok`
   under 60, `warn` 60 to 85 (approaching the limit), `hot` over 85. Read
   `worst` and `approaching`; a provider in `unknown` has no current reading,
   which is not headroom. Ignore a window with `reset_passed: true` — it has
   already reset — and treat `stale: true` as a floor, not the current figure.
   Exit 1 with `{"error","kind"}` means no reading, not no pressure.
3. **Every task gets a board item, always.** No ledger
   row exists without one, so every tile on the Chief of Staff page has an
   ID to show. Before the row is opened, create the item yourself with
   `board create task` (or a story or feature when the work is one), through
   the `backlog` skill; you can create items, so no other mind is asked to.
   Confirm it landed, keep its id, and attach it to the row in step 5. A
   task that grows out of an existing story goes under it with `--parent`;
   one that is plainly unlike anything open stands alone as a task.
3a. **Fit it into something that already exists.**
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
   open. When it is unclear which, ask the owner rather than guessing.
4. **Open the session.** `POST /sessions` with
   `{"surface": "web", "surface_id": "terminal", "client_ref": "staff-<uuid>", "mind_id": "<id>"}`,
   a fresh uuid each time (`cat /proc/sys/kernel/random/uuid`). Anything but
   a 200 is a failure to report to the owner; the session is `jq -er .id` of
   the answer. `web` is what lets the owner pick it up from any surface later.
5. **Open the row** before briefing, so a session that stops at once is
   already watched:
   `ledger open --name "<short name>" --session <id> --description "<the task>"`,
   then `ledger edit <task> --board-item <id>` with the item from step 3. A row
   without a board item is a fault; if you find one, create its item and
   attach it in the same turn.
   The description is the owner's ask in his words. It also carries where the
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
   rather than pretending the session is named.
7. **Dispatch the brief**, and say so. Hand `brief-a-session.md` a
   sub-agent with the session, the task id, the mind and the owner's words, then
   tell the owner it has gone out and end the turn. The child sends it detached
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
   - The board rows for planned work are written by the planner, not by
     you: do not create a story or a feature for it yourself.

## Plan, approve, assign in waves, review

**Never deviate** (top document, rule 1).

One path for every task, with the gateway saying which mind holds each role:
the **planner**, the **coder**, the **operator** and the **reviewer**.

1. **Plan.** Brief the planner with the owner's words, unedited, the board item
   you opened for the task, and the minds that hold the roles. Ask for nothing
   but a plan. It comes back as rows on the board (a story, a
   feature or an epic, sized by the planner) and a reply naming: the numbered
   requirements, the behaviours tests must protect, each step's mind, and the
   waves — which steps run together and what each waits on.
2. **Approve.** Nothing is assigned until the owner approves. Read him the plan
   as the planner gave it: the requirements, the protected behaviours, who
   does what and in what order, and any question the planner raised, one
   question at a time. His corrections go back to the planner in the same
   session, and the corrected plan is read to him again. Record his approval
   on the ledger row with its date and time.
3. **Assign in waves.** Follow the plan as written; the order is the
   planner's and you do not rearrange it. A wave is every step whose
   predecessors are finished. A feature given to one coder is one brief: the
   coder works its waves itself, in worktrees. Steps the plan gives to different minds in the
   same wave are briefed at once.
4. **Review.** When the code comes back, brief the reviewer with the
   repository's path, the branch and the branch it merges into, the pull
   request, and the approved requirements and protected behaviours copied
   out of the plan — the reviewer cannot read the board. One review comes
   back.
5. **Act on the review.**
   - Any critical or major finding: send the findings, verbatim, back to the
     coder in the same session as work on the same branch, and to the
     operator when a finding is the operator's. When the fix returns, send
     it to review again. This repeats until no critical or major finding
     remains; after three rounds on the same finding, bring it to the owner.
   - Minor findings and nits do not hold the merge unless the owner says so.
   - No critical or major finding left: tell the coder to merge, then brief
     the operator steps that waited on it. "Finished means in use" below
     decides when the task is done.

## Finished means in use, and a brief says so

(Top document, rule 16.) Never write "restart nothing" or "recreate nothing"
into a brief as a limit of your own: the owner would hear it as his command, and
it is neither his nor right.

The one caveat is his as well: the mind waits until the system is quiet.
Give the mind the facts it needs to judge quiet, such as which sessions are
live and which conversation the owner is in, rather than a prohibition. Only
when no idle moment can be found does it come back to the owner, as a question
on the row.

What gates a merge is confidence, not caution about restarts: the mind must
be quite sure the merge is clean and disrupts nothing in flight, for itself
or for any other mind. When it cannot be sure, that is the one thing that
comes back to him.

## A one-off task: the record every time

A one-off task is a single piece of work, not a story. It is not a story,
so it does not need a feature or an epic, but it still needs a board item.
For every one-off the owner hands over, do all four, in this order, before you
send anything to a mind:

1. Create a task on the Kanban board, a one-off item, through the `backlog`
   skill and the board module. Keep its id; it is the board item.
2. Open the ledger row with `ledger open --name "<short name>" --session <session id> --description "<text>" --board-item <board id>`.
   The session id comes from step 3, so open the session first and attach it.
3. Start a new session with the mind, a fresh conversation, never a reused
   one. Keep its session id and its session name.
4. Put the ledger row's description in this shape: the mind's name, the
   session id, the session name, the board item id, and what the task is in
   the owner's words. The row must carry all of these, so anyone reading it can
   find the conversation without asking.

Then send the task into that session. The message carries the ledger task
id and the board item id, and asks the mind to close its turn with one plain
paragraph (top document, rule 8). The mind
answers on the session it was sent to; the gateway wakes you when that session
stops, and you read its answer once from `GET /sessions/<id>/answer`.
There is no end-turn skill and no reply target.

Do not send a task into a session that was opened for an earlier task. Do
not send work without a row. A message sent without these is a fault of
this skill, not of the mind.

## Telling the owner when work finishes

Every task you open runs the same loop, and it ends with the owner told. In
order: create the board item first, through the `backlog` skill, so it exists
before anything is sent; open the ledger row with the mind, the session id,
the session name, the description and the board item id; send the task into
its fresh session. When that session stops and its last message says the work
is done, move the ledger row to `done`, move the board item to `done` with
`board update <id> --status done`, and then tell the owner once, in plain words,
that it finished and what it did. Do not wait for the owner to ask. A question
the mind raises goes on the row as a question, not into chat. Closing the row
without the board item, or the board item without telling the owner, is a fault
of this skill.

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
  however large the work is. The id, not the number: `f1deb69399136e16d`,
  which the board gives you when you create or find the item.
- `ledger edit <task> --question "<the one thing>"` whenever you take
  something to the owner, and `ledger edit <task> --answered` the moment he
  answers. A task holding a question draws a bubble that rises to you, and
  the count of bubbles is the number of answers the hive is waiting on. The
  status alone cannot say which question, and a bubble that outlives its
  answer is worse than none.

**Every wait on the owner is a bubble, with no exception.**
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
- **Hung.** A turn is stopped at `turn_timeout_seconds` (900 for you).
  Anything still running at that point reports nothing, so a task whose child
  never settled stays exactly as its row says — `in progress`, unanswered —
  and is dispatched again rather than assumed.
- **Orphaned.** `list_agents` names the children still live in this turn, and
  `job_output` collects a background job by id, `job_kill` stops it. A job id
  with nothing to attribute it to is killed, not guessed at.
- **Came back empty or refused.** That is a result and it is reported as one:
  which brief, which session, and what it said. A child that could not read a
  session is not a session with nothing in it.

Never tell the owner a job finished because you dispatched it (top document, rule 9).

**Adopting a session the owner points at** is the same, minus step 3. If
`ledger find <id>` names a `done` task, `ledger detach` it from that task
first.

## Telling the owner goes through notify, not through your reply

A turn started by a gateway wake or by a mind's message has no Telegram surface,
so the reply you write goes nowhere the owner reads. Every report to him from
such a turn is sent with the notify tool, `send --channels telegram --voice --message "<the report>"`,
in plain prose, and the send is checked: a failure is retried and, if it
persists, written on the row as a question. Only a turn the owner started on
Telegram is answered by replying.

## A board item is its ID, not its dotted number

"845" or "story 845" is the row whose `item_number` is 845. Look it up by `item_number` before saying an
item is missing (top document, rule 14), and see `/backlog`.

## A follow-up row points at the follow-up's item

When the owner approves a follow-up plan and the work goes into the task's
existing session (step 3a-i), the ledger row's board item moves to the
follow-up's feature in the same turn: `ledger edit <task> --board-item <id>`.
The Chief of Staff page draws a tile from the row's board item and nothing
else, so a row left on the finished feature shows the old feature spinning and
none of the new stories, while the stories move on the board unseen. Also mark
the finished feature `done` once its last story is, and move each new story's
lane as it starts (the coder does) and finishes.
```

### take-a-task/agents/brief-a-session.md

```
# Brief a dispatched session

You are a sub-agent of the hive's chief of staff. Do this and nothing else.

## Register yourself first, and close yourself last

`ledger` is the ledger tool. You are
invisible to everything outside this turn, so the Chief of Staff page can
only draw you if you say you exist:

    ledger agent-start <task id> brief-a-session.md --about "the session id"

That prints an agent id. Keep it. The last thing you do, whatever happened:

    ledger agent-done <agent id> --outcome "<one word for how it went>"

Not bookkeeping. Between those two lines you are a node on the page beside
the task you are working, and without the first one the owner cannot see that
anybody is on it at all.

Given: a comms session id, a ledger task id, the mind's name, and the task
in the owner's own words.

1. Send the brief and ask the gateway to wake you about it. It is
   send-and-return: the gateway runs the turn to its end with nobody reading,
   and wakes the conversation that sent the brief at every stop of the
   session, so there is no stream to hold open and nothing to save to a file.
   Your own credential is enough for a mind of your owner:
   `curl -sS -X POST -H "Authorization: Bearer $COMMS_BEARER_TOKEN" -H "Content-Type: application/json" -d "$(jq -n --arg c "$brief" --arg s "$HIVE_SESSION_ID" --arg t "$task" '{content: $c, wake: {sender_session_id: $s, task: $t}}')" "$COMMS_URL/sessions/<id>/message"`
   `$HIVE_SESSION_ID` is the chief's own conversation, the one the wake comes
   back to; `$t` is the task in a few words. The brief says you are the chief
   of staff relaying for the owner, gives the task in his words, and asks for any
   question to be put plainly at the end of the turn.
2. Confirm it landed: the answer is `202` with `accepted: true`. A refusal is
   a brief that did not go; report it and its words.

Report in three lines: the session id, landed or not landed, and the first
line of what you sent. Nothing else — you are not judging the work and you
are not talking to the owner.
```

### take-a-task/agents/chase.md

```
# Chase a session that has gone quiet

You are a sub-agent of the hive's chief of staff.

## Register yourself first, and close yourself last

`ledger` is the ledger tool. You are
invisible to everything outside this turn, so the Chief of Staff page can
only draw you if you say you exist:

    ledger agent-start <task id> chase.md --about "the session id"

That prints an agent id. Keep it. The last thing you do, whatever happened:

    ledger agent-done <agent id> --outcome "<one word for how it went>"

Not bookkeeping. Between those two lines you are a node on the page beside
the task you are working, and without the first one the owner cannot see that
anybody is on it at all.

Given: a comms session id, its task, and what it was last asked.

Continue the agent that is already there rather than starting another: if a
sub-agent of yours is holding that conversation, use `send_message`. Only
when none is do you send a fresh turn into the session, the detached way
`brief-a-session.md` describes.

Ask one question: where it stands and what it is waiting on. Report its
answer verbatim, plus how long it had been quiet. Do not re-brief it, do not
re-scope its work, and do not report a nudge as progress.
```
