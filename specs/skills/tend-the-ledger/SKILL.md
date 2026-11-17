---
name: tend-the-ledger
description: Keep every watched task moving and give Daniel the sitrep. Trigger when woken because a watched session stopped, when Daniel asks about a task or a session, answers a question you brought him, says he'll handle a session himself or that you can watch it from here, or asks for a sitrep ("sitrep", "sit rep", "where do things stand", "what's working").
---

# /tend-the-ledger

`ledger`, the comms calls and the detached, confirmed send of a message
into a session (`POST /sessions/<id>/message`) are as in `/take-a-task`.
The ledger and the sessions are the truth, never your memory of them.
Your own markers live in `${DSH_SPAWN_DIR:-$HIVE_PROJECT_DIR/minds/$MIND_NAME}/data/`.

Statuses: `in progress` (working, watched), `waiting on response` (Daniel
has been asked), `on hold` (a question held overnight), `with Daniel` (he
took it), `done` (reported finished). Every move names the status you read
it in: `ledger set-status <task> "<new>" --expect "<old>"`. A refusal means
it moved under you; read it again.

The watch wakes you. A tick beside you compares `GET /sessions/live` against
the rows every minute and drops one message naming every task whose sessions
have stopped, so a wake is already the list and you never hunt for it. It
records on the row when they stopped (`stopped_at`) and when you were told
(`woken_at`), both in `ledger show`, and it never wakes you mid-turn: wakes
that land while you are talking arrive together on the next tick that finds
you idle. It sets no status, ever — every status is yours.

## A watched session stopped

1. `ledger find <session>`. No task, or a `done` one: not watched.
   `with Daniel`: leave it, never answer or mention it.
2. Hand each one `read-and-judge.md` — one sub-agent per session, all at
   once, as `/take-a-task` describes. Reading a transcript is the step that
   would otherwise fill your own context with somebody else's work.
   A task whose description says a feature awaits your check gets
   `check-a-feature.md` instead. A board item it now covers goes into the
   description.
3. Tell Daniel what the session said and what you dispatched, and end the
   turn. The mind's closing answer is the last `result` event in the stream
   saved under `data/turns/`: read that one event yourself, in the same turn
   as the wake, and give Daniel its verdict and any question it ends on,
   marked as the mind's own words that no reader has checked yet. It is one
   event, not the transcript, so it costs little context. The reader checks
   it, and its report follows as a correction or a Boop. Never make Daniel
   wait on a reader for an answer already sitting in a file: on 16 November
   a ninety-second answer reached him fifteen minutes late, and only because
   he asked (Daniel, 2026-11-16). With no saved stream, or no result event
   in it, say so and say that a reader has gone in. Never hold the turn open
   waiting for the reader.
4. As each child reports: whatever the task already answers — Daniel's
   instructions, the description, the board item it names — answer in the
   session yourself. Pointing the agent at the story that settles it is an
   answer. Sending that answer is a `brief-a-session.md` child too; deciding
   it is yours.
5. Bring Daniel only what the task does not answer, once: which task, which
   mind, and the question; the row goes to `waiting on response`, and the
   question itself onto the row with
   `ledger edit <task> --question "<the one thing>"`. The status says an
   answer is owed; only that column says which, and it is what the Chief of
   Staff page draws as a bubble rising to you. A
   task reported finished by its assigned mind is closed: trust its completion
   report without a redundant independent verification turn; move it to `done` and
   tell Daniel it is done, once, so he can catch a miscommunication. Do not
   ask permission to close it (Daniel, 2026-11-12). Then close it out as
   **Work that has finished** below says.
6. Whatever this taught you about the mind goes under its **Chief's notes**
   in `<skills directory>/take-a-task/roster.md`. Its **Role** is Daniel's
   and you never edit it.

## Work that has finished

Every row that reaches `done` ends with its sessions ended, and this holds
however it got there: you closed it on the mind's completion report, Daniel
said it was finished, or you noticed it in a sitrep. It used to be written
into the stopped-session flow alone, so a task Daniel closed in conversation
kept its session open for good — the rule is the status, not the route to it.

One `close-a-finished-session.md` child per `done` row whose sessions are
still open, dispatched together. The child asks the mind to run its own
`end-session` skill, waits for it, and only deletes the session if the mind
will not. Asking is the whole point: a finished task's session otherwise sits
open forever holding a model and a context window, and deleting it outright
throws away what that mind learned doing the work, because only the mind
inside a conversation can read it and write its own memories. There is
nothing unsafe about asking an agent to run a skill that ends its own session
(Daniel, 2026-11-14).

The child never closes a session Daniel is in — it acts only on the `web`
sessions you opened — so a conversation on his phone or terminal is reported
as left alone. Left alone is not a decision, it is a question: when the child
reports one, ask Daniel whether you may close it, naming the task, the mind,
the session and its surface, and put it on the row with
`ledger edit <task> --question`, clearing it with `--answered` when he
replies. On his yes, ask the mind to run `end-session` as the child would; on
his no, leave it. Never settle it by deciding not to close it (Daniel,
2026-11-16). A session reported `closed without a
harvest` means that mind has no `end-session` skill; say so in those words
rather than reading it as an ordinary close, and put it under the mind's
**Chief's notes** in the roster.

## A recreate or restart report closes its row

When your own recreate tool or the operator mind reports a recreate or
restart as done, that
report is itself the finish of the task it was requested for (Daniel,
2026-11-16). The same turn: read the row it names, check the thing the
restart was for, and move the row to `done` if it is verified, then close
out its sessions as **Work that has finished** says. Do not hold a row open
to wait for something that will only happen later, such as a first saved
training row: verify it now, and if it cannot be verified yet, say exactly
what is outstanding and move the row to `done` anyway with that written in
its description, or open a new row for the check. A row left `in progress`
after the work it tracks has landed is drawn on the page as live work, which
is wrong.

## A session that has gone quiet

A task `in progress` whose session has produced nothing for long enough that
you notice, and which the watch has not reported as stopped, is chased rather
than guessed at: hand `chase.md` a sub-agent with the session, the task and
what it was last asked. Say that you are chasing it, end the turn, and report
what comes back. A nudge is not progress and is never reported as any.

## Daniel answers

Drop his answer into the session that asked, in his words, move the row back
to `in progress` (from `waiting on response` or `on hold`), and clear the
question with `ledger edit <task> --answered`. A bubble that outlives its
answer is worse than no bubble at all. When it is unclear which waiting task
he means, ask.

A relay carries his words and the facts the mind needs to act on them, never
a limit of your own on restarting: confirmed work is merged, rebuilt and
restarted at the first quiet moment, as **Finished means in use** in
`/take-a-task` says (Daniel, 2026-11-17).

## Hand-over

Only on his explicit word, never from his going quiet.
- "I'll handle it": move it to `with Daniel` and give him the session id
  and mind; he can pick it up from any surface.
- "Watch it from here": back to `in progress`, then read it as above.

## Sitrep

Any time he asks. `ledger list`, then one `gather-for-sitrep.md` sub-agent
per task that is not `done`, dispatched together — they are independent and
the slowest one should not set the pace. Say how many went out, end the turn,
and assemble their answers into the report as they land. Report:
1. Questions waiting on him (`waiting on response` and `on hold`), each with
   its task, mind and question.
2. What moved since the last sitrep — rows whose `updated_at` is after the
   time in `last-sitrep` in your data directory — finished work first, and
   what each one actually did read from its blocks
   (`GET /sessions/live/text`, or `GET /sessions/<id>/history` once the
   buffer has aged out), never from the row's description.
3. What is in progress, and what is with him.

Then bring the rows you have just reported off hold: an `on hold` task whose
question you have now put in front of him goes to `waiting on response`
(`--expect "on hold"`), because holding it was for the night and the night
is over. One you did not report stays where it is. `with Daniel` is never
touched.

Then close out the finished ones. Any `done` row still holding an open
session gets its child per **Work that has finished**, in this same turn: a
sitrep is the one pass that looks at every row, so it is the last place a
session left open can go unnoticed.

Then write the current time (`date +%s`) to `last-sitrep`.

## Boop when a report adds nothing

A helper's closing note that only repeats what you already told Daniel (a
duplicate completion notice, a second confirmation of the same landing or
closure) gets the single word "Boop." as the whole reply, because a turn must
produce some reply and the shortest one costs the fewest tokens. Say anything
more only when a report changes something: a new result, a new question, a new
blocker, or a status you moved. (Daniel, 2026-11-14.)

## Daniel's feedback on how you work

Feedback from Daniel about how you do your job is an instruction to update
your skills, not a remark to acknowledge. Write it into the skill it
concerns (here, take-a-task, or the roster's standing preferences) in the
same turn, in your reference copy and the copy your harness loads, and tell him
it is written. (Daniel, 2026-11-12.)

## Every wait on Daniel is a bubble

Whatever you are waiting on him for, in this skill or anywhere else, is a
question on a row the same turn you ask it (Daniel, 2026-11-15). A feature
awaiting his word on anything, such as a go-ahead, a timing, a gate to verify
or a read-back sign-off, is a bubble, not a line in chat. If no row exists to
carry it, open or attach one first. Then `ledger edit <task> --question`, and
`--answered` when he replies.

## Precision when you bring Daniel a question

He cannot answer what he cannot identify. Name the exact thing: the repo
or checkout by name and path, the host, the file, the field, the board
number, the session. Never "a second repository" or "a config". If you do
not know the precise name, ask the agent for it before bringing the
question. (Daniel, 2026-11-12.)

## Roles are not decided by tool access

Whether a mind may create or only read board items is set per request, not
as a standing role. Do not carry "who owns the writes" questions to Daniel.
(Daniel, 2026-11-12.)
