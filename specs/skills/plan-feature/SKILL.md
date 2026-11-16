---
name: plan-feature
description: Turn a feature Daniel wants into a planned feature on the hive board — requirements interrogated one question at a time and read back for approval, then a short high-level list of the behaviours tests must protect, read back too, then split into stories ordered for parallel work, each written by its own delegate. States behaviour only and never reads the codebase; that is /code's first phase. Trigger when Daniel asks for a feature to be planned, broken into stories, or added to the backlog as more than a single piece of work; and before any build begins.
user-invocable: true
---

# /plan-feature

Planning ends with rows on the board, not with a document. `board` below is
`HIVE_SITE_URL=http://127.0.0.1:8710 python3 ~/Storage/Dev/hive_mind/tools/stateless/board/board.py`, with `BOARD_PUSH_TOKEN` sourced from `~/Storage/Dev/hive_mind/.env`;
`/backlog` carries its full surface. A feature needs an epic to hang from —
if Daniel has not named one, ask which, and ask nothing else in that turn.

This is where the specification is agreed. There are exactly two stops, both
here: the requirements read-back in phase 2 and the protected-behaviour list
in phase 3.
`/code` runs autonomously from what this skill wrote down and comes back to
Daniel only on a problem it cannot decide — so a requirement left vague here
is a requirement nobody asks about before it is built.

**Plan from what Daniel says, not from the code.** This skill does not read
the codebase, and the mind running it usually cannot: the planning mind sees
its own repo at most, and a feature nearly always lands somewhere else.
Reading the code is `/code`'s first phase, where the checkout is actually in
reach. So state requirements and stories as **behaviour** — what the feature
does, refuses, persists and shows — and name no module, function, file or
test. If the only way to say a requirement is to point at code, it is not a
requirement yet; ask Daniel another question instead.

Two consequences worth saying out loud. A plan agreed here can turn out to
disagree with the code, and it will also be **incomplete** — a mind that
cannot read the code cannot know about the migration, the caller or the
failure path the module's own conventions demand. Neither is a defect in
this skill and neither is a reason to guess. `/code` phase 1 completes the
list from the code, decides what the approved requirements already
determine, and takes back to Daniel only what genuinely changes or widens
what he approved. Do not pad the list here with speculation to cover for it.

## 1. Interrogate the requirements

Ask until the requirement list is complete, and expect to ask more than
once: Daniel's first description is never the whole thing, and accepting it
is how features ship wrong. Ask about what he has not said — what he sees
when it works, what happens when it fails or is empty or is denied, what
persists and where, who is allowed to do it, and what is explicitly out of
scope.

**One question per turn**, across as many turns as it takes. There is no
exception: a numbered list of questions in a single turn is not acceptable
even when it would be faster. Daniel works in parallel and has forgotten the
top of a long turn by the time he reaches the bottom, so a stacked list
forces him to answer the wrong question or re-read. Pick the most blocking
one, ask it, park the rest.

Anything you can decide yourself is not a question. Decide it, state the
decision, and spend the turn's one question on the part that is genuinely
his. Push back on a vague answer once; if he says "just do it", proceed with
the gaps written down as assumptions on the list.

Stop when the next question would only confirm something already answered.

## 2. Read back the requirements

Numbered. Non-technical. Each one an outcome Daniel can check himself
without reading code:

```
1. There is a page that shows the current configuration.
2. The configuration page has a button that refreshes the data.
3. Refreshing shows values as of the moment the button was pressed.
```

Not "add a GET /config route", not "wire the refresh handler to the store".
If a requirement needs a technical word to state, it is two requirements or
it is an implementation detail that does not belong on the list.

**Stop here and wait for Daniel's approval.** Nothing goes on the board and
no test is described until he has signed this list off. This is the first
of exactly two stops, and both of them are in this skill.

## 3. Name the behaviours tests must protect

**You are not writing tests and you are not describing tests.** You name a
short, high-level list of behaviours worth protecting against future change,
Daniel approves that list, and `/code` turns each one into whatever test
actually protects it. Judging a test is reading the code — can it import and
call the thing, would it fail if the behaviour broke, does it pass for the
wrong reason — and none of that is answerable from here. The test standard
lives inside `/code`, in that skill's own directory, because `/code` is the
only place tests are designed; it is not this skill's to read or to apply.

A protected behaviour is a statement about what the system does that you can
believe has a code path behind it: something looked up, decided, selected,
stored, refused or returned. Daniel's own example — when an agent has an
icon, that agent's icon is the one shown. Behind that there is a path taking
an agent id and returning its icon, and a path like that can be wrapped. That
is the test: not whether it is the right behaviour, but whether something in
the code plausibly *does* it.

**High level, and few.** Three to five for most features. Not one per
requirement, not one per story. If the list starts reading like a checklist
of every approved outcome, it has turned into a test list and has gone too
far down; pull back up to the handful of behaviours whose silent breakage
would actually hurt.

Read them back numbered, as plain sentences:

```
1. An agent that has an icon shows that agent's icon.
2. A spawn always resumes the conversation it was given, never a new one.
3. A request without a valid token is refused rather than served.
```

What does not belong on the list: appearance, since "the button is green" has
no code path to wrap (though "the button turns green once the save succeeds"
does); anything phrased as a function, route, file or module; and anything
only true at runtime against a real service, which `/code` verifies by
running it and reports at ship time.

When a change warrants no protected behaviours at all — a skill, a document,
a configuration value, a removal — say so in a sentence with the reason.

**Stop here and wait for Daniel's approval.** This is the second and last
stop in the whole build; nothing after it asks him anything. `/code` may
later report that a behaviour on this list cannot be reached by a test. That
is a legitimate answer and it is reported at ship time, not a defect in this
list — but a test protecting a behaviour he never approved does not get
written, which is why the list is his to sign.

## 4. Split it into stories

A story is one behaviour, buildable and verifiable on its own. Split on
behaviour rather than on layer: a story that ships a schema with no caller
cannot be verified and is not a story. Split on what Daniel described, since
that is all this skill knows — the order below is a claim about behaviour
depending on behaviour, and `/code` corrects it against the real code when it
turns out the files disagree.

Look deliberately for work that can run at once. Two stories are parallel
when **neither needs the other's outcome to be verifiable** — when Daniel
could check one of them without the other having happened yet. That is a
judgement about behaviour, which is all this skill can see. Whether the
files actually collide is not knowable from here; `/code` phase 1 finds that
against the real code and reorders the stories when the dependencies
disagree with this one. Say which you believe are parallel and why, because
the order is what decides whether this feature takes a day or a week.

Assign every approved protected behaviour to the story that delivers it. A
protected behaviour reaching no story is a behaviour nobody protects, and a
story carrying none says so explicitly.

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
--epic <id> --description D`), with the approved numbered requirements and
both diagrams in its description. Then give one delegate per story the brief
in `briefs/write-a-story.md`, filled in with the feature id, the story's
behaviour, its place in the order and what it waits on, and the protected
behaviours assigned to it. Run them at once — that is the point of having split the
work.

The board is the only thing `/code` reads. A protected behaviour that does
not reach a story description is a behaviour nobody tests.

When they return, read what they wrote: a delegate that misunderstood a
story writes a plausible row, and the board is what gets built from.

Report the feature's backlog number, its stories in build order, and which
of them can start immediately.

Approving this plan authorises the whole feature. `/code` then builds every
story in it, to merged and pulled, without asking again — a story is not a
separate permission, it is part of the thing already agreed. The only
stories it may hand back are the ones this plan marks as needing Daniel, and
those come back as a list of exact questions once the rest is built (Daniel,
2026-11-16).
