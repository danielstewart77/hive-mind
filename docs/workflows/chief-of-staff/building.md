Stage document for the coder role; it is the text of the skills below with this install's specifics removed.

## feature/SKILL.md

---
name: feature
description: Deliver a software feature or an explicitly ordered list of stories, a wave at a time, with stories in one wave built simultaneously in separate Git worktrees, durable resume and integration checks. Not for feature planning alone or implementation of one story.
---

Follow the [skill announcement contract](../task/references/observability.md) for entry, handoff and exit.

Announce `feature` before acting. Resolve the selected stories, their dependencies, the repository and the authorised integration target from the feature's row on the board (`backlog` skill: `board show <id>`, `board list --parent <id>`). The feature's description carries the approved requirements, the protected behaviours and a build-order diagram; each story says what it waits on and which stories it runs beside. That order is a claim about behaviour made without reading the code: check it against the code, correct it when the files disagree, and say what you changed. Stop for a contradictory dependency or a missing target. A named list of stories does not authorise expanding to every sibling. Partition unrelated repositories or targets without losing cross-group dependencies.

## What you decide yourself

Testing against an isolated copy you start from your own branch needs no permission; the running services are never restarted, rebuilt or pointed at by you, and an isolated copy never shares their ports or their database. Two stops in three minutes once asked what the plan already answered (top document, rules 3 and 4).

## Waves

A wave is every story whose predecessors are integrated and whose owned files collide with no story already running. Work a wave at a time:

1. Persist the run before implementation. Use the bundled [manifest and worktree helper](scripts/swarm.py) under its [run contract](references/run-contract.md): story ids are the board's item ids, `feature_branch` is a local integration branch cut from the repository's main branch, and `workspace_root` and every artifact live under the install's private worktree scratch folder, in a folder for this feature, never inside a source checkout. Where the contract says ADO, read "the board". Report the manifest path.
   **Own files, not folders.** Before persisting, read the code and give each story the files it will change and the new files it will add, by path. Name a folder only when the story really rewrites most of it. Two stories queue behind each other only when they change the same file; once every story of a feature was given the whole gateway folder, so a ten-story run could work one story at a time and the wave plan meant nothing. When a story turns out to need a file it does not own, update its ownership through the helper rather than declaring wide at the start.
2. Ask the helper for the queue. Start every eligible story: the helper gives each its own branch and worktree.
3. Launch one thread per started story, all in the same turn, each told to run `story` in that story's worktree. Give each the story id, its worktree and branch, its starting base, the paths it owns, and the instruction that it is not alone and must preserve other threads' edits. Read the harness's current thread limit first; a story that cannot get a thread stays queued, it is not blocked.
4. As each story returns an accepted, reviewed candidate, record it and integrate it into the feature branch — one story at a time, never two at once. Verify the candidate is in the feature branch's history before recording it integrated.
5. When the wave is integrated, run the affected checks on the feature branch, then ask for the queue again. Repeat until no story is pending.

Stop the affected line at the first failed or blocked story; preserve prior work, and let stories that do not depend on it finish. On resume, reload the manifest, verify the target and prior integrations, and restart at the first incomplete story. Never reset a failed review or expand scope to make the gate pass.

## Finishing

Run feature-level verification on the integrated feature branch. Use the [shared gate](../task/references/completion.md) before reporting completion.

A finished feature is the feature branch pushed and one pull request opened per repository against the main branch (top document, rule 17). Report the pull request and the manifest path (top document, rule 21). Critical or major review findings come back to you as work on the same branch.

## feature/references/run-contract.md

# Run and job contract

Use Python 3.10+ and Git. Invoke scripts/swarm.py with an explicit manifest path.
The helper is local-only; the coordinator owns board reads, scheduling, tests, and integration.

## Initialization config

JSON fields: feature_id, repo (absolute Git checkout), feature_branch (local integration
branch), workspace_root (absolute dedicated directory), stories. There is no story-count cap.
Legacy `capacity` fields are ignored; they are not runtime agent limits. New configs omit them.
Feature/story IDs and revisions are positive integers. Each story has id, revision,
predecessors (integer story IDs), ready (boolean), and owns (literal repo-relative file/subtree
paths; a dot reserves the whole repository). No ownership globs or traversal paths. Include
every unmet in-scope predecessor; graph validation rejects missing IDs and cycles. Keep the
full source graph and verified already-delivered prerequisite evidence in run artifacts.

Example structure (replace example values with actual inspected inputs):

```json
{
  "feature_id": 15606,
  "repo": "/path/to/repo",
  "feature_branch": "feature/15606-reporting",
  "workspace_root": "/path/to/scratch/feature-15606",
  "stories": [
    {"id": 15691, "revision": 7, "predecessors": [], "ready": true,
     "owns": ["app/usage", "tests/test_usage.py"]}
  ]
}
```

Example paths and revision are illustrative, not facts about feature 15606. Do not run this
example unchanged. Keep config/evidence/artifacts outside source worktrees. The helper's
manifest is authoritative for local state; edit it only through supported commands.

## Commands

Prefix each command with: python <skill>/scripts/swarm.py --manifest <absolute-json-path>

| Command | Purpose |
|---|---|
| init --config FILE | Validate config/graph and persist a new run; an existing manifest is refused. Resume it through other commands. |
| queue | Report eligible pending stories respecting readiness, dependencies and ownership, without a story-count cap. |
| start STORY | Create/resume the isolated story branch/worktree and pin its starting base. |
| candidate STORY --base SHA --head SHA | Record a clean committed candidate; invalidate prior review. |
| review-record STORY --base SHA --head SHA --evidence FILE | Record evidence only for the current candidate and source revision. |
| mutation-create STORY | Create/resume an exclusive detached candidate worktree for mutation review. |
| mutation-remove STORY | Remove only the clean registered mutation worktree; refuse dirty cleanup. |
| block STORY --reason TEXT | Record a real blocker while preserving worktree and ownership. Slot waiting alone is not a blocker. |
| unblock STORY | Resume after coordinator resolves the recorded blocker. |
| reconcile STORY --source FILE --stopped | Apply an approved source/readiness change after stopping the worker; invalidate old candidate/review evidence. |
| record-integrated STORY --validated-head SHA --evidence FILE | Verify feature tip, review, ancestry and validation evidence before marking integrated. |

Use --help for exact parser options. Nonzero exit means the operation was not accepted;
inspect the saved manifest and Git state before retrying. Never use --force to bypass a refusal.
The helper validates attestations and pins; it cannot prove a reported command actually ran.
It does not count or dispatch runtime agents. The coordinator maintains queued stage work
alongside the manifest, checks live slot availability, and dispatches only when capacity is
available. A manifest story marked active may be waiting for its next agent stage. No helper
command turns a story reservation into permission to exceed the runtime agent limit.

## Worker envelope

Pass story/change ID, requirement source and revision, documents path, worktree and branch,
starting base SHA, current review base if updated, owned paths, predecessor contracts,
test environment/database, agreed requirement/test status, and the repair ladder ID,
authorization, current tier/submission slot, remaining allowances, and separate local
validation count/limit. Initial implementation identifies its preparation before any ladder.
Every worker is told it is not alone and must preserve other agents' and user's edits.
The coordinator records runtime agent IDs alongside job artifacts for resume checks.
Planning-to-coding handoff includes the accepted plans, decisions and relevant code paths;
it does not depend on reusing the planner as the implementor. Include the Behavior design
section/path, counterpart inventory and relevant FINDINGS.md IDs/current dispositions in
planning, coding and review jobs. Reviewers receive pinned design/requirements and evidence,
not the builder's conversation as a substitute for independent inspection.

## Shared behavior and findings

PROTECTIONS.md's technical section records relevant shared behaviors, counterpart paths,
story/contract owners and intentional differences. Each affected IMPLEMENTATION.md carries
the scoped Behavior design and UNIT-TESTS.md traces cases to it and accepted protection IDs.
These are existing planning artifacts, not new helper manifest fields or approval rounds.

Keep coordinator-owned FINDINGS.md alongside the manifest. Each entry records a stable ID,
story/requirement or protection link, affected behavior and counterpart paths, first report
and source pin, current reviewed pin, severity, blocking status/rationale, owner, disposition
(open, fixed, dismissed with evidence, or deferred optional suggestion), and verification
evidence/fix commit where applicable. Append or retain disposition history across reviews.
Do not defer a blocking agreed-behavior violation solely because it is labelled Minor.
This ledger tracks defects and decisions, not a backlog of invalid tests awaiting disposal.
Material design changes route through planning/spec reconciliation. The helper does not manage these artifacts.

## Pending questions and continued work

Keep coordinator-owned `questions.jsonl` outside the source worktrees. Append events rather
than overwriting history. Each question records its ID, timestamp, story/criterion/action
scope, concrete question shown to the owner, supporting source/revision, recommendation if
grounded, delivery evidence, and pending/answered status.
Answer events record the owner's actual answer and source, affected requirement reconciliation,
and the action resumed. A question drafted in a document but never shown to the owner is not
an asked question. Never treat elapsed time, silence or a preselected option as an answer.

Record which exact actions await each answer and which independent work remains eligible in
the stage queue. Include pending question IDs and continued-work instructions in worker
envelopes. Use helper block only for the genuine dependent job/gate; a question is not permission
to erase work, release ownership, skip predecessors, invent rules, reset repair counters, or
stop the whole run. Preserve useful partial discovery/planning, and dispatch additional
independent work through the required role. A FAIL result with missing information requires
the coordinator to ask a concrete question and continue scheduling, not merely report a blocker.

Consume asynchronous answers during the same run and resume affected actions once valid;
do not wait for a new command to continue. If no independent action remains, keep the actual
question pending and wait for input with concise progress updates.

Every job also carries PROTECTIONS.md's path/version, the story's stable protection IDs,
linked requirement source/revision, and human acceptance evidence. Keep this coordinator-owned
artifact outside source worktrees, separate from the helper manifest.
UNIT-TESTS.md maps executable test IDs to protection IDs. Additional
cases within accepted behavior do not reopen approval. Workers return materially new
protections or changed outcomes to the coordinator for batched human review. Pending
acceptance blocks dependent implementation, not independent accepted work.

## Dispatch evidence

Keep `dispatches.jsonl` alongside the run manifest, outside source worktrees. This is a
coordinator-owned record of actual tool calls, not a new swarm.py command or a field to
manually inject into its manifest. Append one record per spawn or follow-up containing:

- UTC timestamp, story ID, stage, source revision, and action (`spawn` or `followup`).
- Exact `agent_type`, absolute agent definition path, and SHA-256 of that file.
- Task/instance label and the runtime agent ID returned by the tool; these are separate
  from the configured type. Failed launches record failure, not a fabricated runtime ID.
- Configured `model` and `model_reasoning_effort`, each null when omitted; record the
  effective model/effort only if the runtime exposes them, otherwise mark them unknown.
- Fork mode, any explicitly owner-requested model override, and artifact/worktree paths.

Verify the definition hash and original type/story before a follow-up. Changed definition
or model settings require a fresh instance with the same durable job/repair budget. A
follow-up cannot change an instance's agent type. Keep earlier records intact on resume.
Before accepting a stage result, verify that its runtime ID maps to the required type.
A generic worker running the same skill is not equivalent.
Preserve historical evidence from earlier runs without inventing retrospective dispatches.

For coding escalation, maintain a coordinator-owned per-story ladder ledger alongside the
manifest; this is not a new helper command or a manually injected manifest field. The
bounded coding repair ladder: initial implementation at the high tier, then
the next tiers up, the models being those the install's file names, with two evaluated repair submissions per tier, six maximum per
authorized ladder. Severity records impact/blocking classification, never model selection.
Keep the following distinct:

- Lifetime candidate/rejection history, actual dispatch/model use, tokens and cost.
- Ladder ID, authorization source/date/scope, initial trigger candidate/report, current tier,
  used and remaining tier/ladder allowances, and status (active, approved, exhausted or blocked).
- Each repair submission's ordinal, tier/model, full candidate SHA and source revision,
  blocking finding IDs/severities, consolidated report and actual outcome. Link all individual
  reviewer reports to that single submission; approval stops, substantive rejection consumes
  one round. Initial implementation and its initial review do not consume a repair round.
- Pending preparations/evaluations and excluded interruptions (infrastructure, unavailable
  dispatch, changed requirements), with reasons and actual cost. Do not assign a substantive
  rejection or advance a submission slot without a consolidated substantive outcome.
- Local pre-review validation count/limit and failed commands, including any `unable-to-submit`
  blocker. Persist this separately from reviewed rounds across resumes and agent replacements.
  A fresh dispatch cannot reset an exhausted preparation; resolve the blocker or obtain an
  explicit decision before resuming. Failed gates do not justify repeat paid review.

Do not reset per finding, downgrade, skip tiers, or create an outer retry allowance. After
the second rejection at the top tier the coordinator holds only that repair, asks for the explicit
owner decision immediately, and continues other eligible work. New owner
authorization creates a separately identified ladder with references to all earlier history;
it never rewrites historical counters or model evidence. Record any authorized migration
from an existing history before dispatch, with the exact selected stories and allowances.
A policy-file edit itself does not migrate or resume a run.

Use the escalated coding step with the ladder's explicit model. Record baseline and escalated
definition hashes separately; a selected-but-undispatched model is not a model used. An
unavailable runtime role remains a blocker, never permission to substitute a generic worker
or silently inherit the top tier. Model changes preserve both lifetime history and ladder counters.

Artifacts: STORY-DESCRIPTION.md (or REQUIREMENTS.md), IMPLEMENTATION.md, UNIT-TESTS.md,
PLANNING-RESULT.md, IMPLEMENTATION-RESULT.md, CODE-REVIEW.md, plus refutation and gate logs.
Result lines remain RESULT: PASS or RESULT: FAIL | reason for compatibility, but documents
and exact pins determine eligibility. The gate cannot have failed or unrun checks.

## Machine-readable attestations

Review evidence JSON contains base_sha, candidate_sha, story_revision, verdict (APPROVED),
checks (a nonempty list of objects with command and exit_code: 0), and unresolved_findings
(unresolved **blocking** findings; empty for approval). Keep nonblocking Minor/Nit preferences
separately in CODE-REVIEW.md; they do not populate this field or block approval. A Minor
functional defect that violates required behavior belongs in unresolved findings. Derive evidence from actual
review and gate evidence, never from a requested
outcome. Retain the report paths and command logs in the story artifacts.

Integration evidence JSON contains validated_head and a nonempty checks list with command
and exit_code: 0 for every required combined gate. Record actual working directories and
logs with the artifact even when the helper only validates the required keys.

Both commit IDs are full object IDs, not moving branch names. A reconcile source file contains exactly id,
revision, predecessors, ready, and owns. The coordinator attests --stopped only after verifying
no worker is active. Reconciliation validates the full graph/ownership, preserves original
starting base/worktree and dirty edits, and clears candidate/review evidence. An active job
remains blocked until explicitly unblocked. Re-plan against the new approved source; do not
overwrite revisions merely to make stale reviews match.
