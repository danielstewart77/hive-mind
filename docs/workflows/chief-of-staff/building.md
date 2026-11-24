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

1. Persist the run before implementation. Use the bundled [manifest and worktree helper](scripts/swarm.py) under its [run contract](references/run-contract.md): story ids are the board's item ids, `feature_branch` is a local integration branch cut from the repository's main branch, and `workspace_root` and every artifact live under the install's private worktree scratch folder, in a folder for this feature, never inside a source checkout. Report the manifest path.
   **Own files, not folders.** Before persisting, read the code and give each story the files it will change and the new files it will add, by path. Name a folder only when the story really rewrites most of it. Two stories queue behind each other only when they change the same file; once every story of a feature was given the whole gateway folder, so a ten-story run could work one story at a time and the wave plan meant nothing. When a story turns out to need a file it does not own, update its ownership through the helper rather than declaring wide at the start.
2. Ask the helper for the queue. Start every eligible story: the helper gives each its own branch and worktree.
3. Launch one thread per started story, all in the same turn, each told to run `story` in that story's worktree. Give each the story id, its worktree and branch, its starting base, the paths it owns, and the instruction that it is not alone and must preserve other threads' edits. Read the harness's current thread limit first; a story that cannot get a thread stays queued, it is not blocked.
   **Every thread says which story it is for.** Whatever you launch for a story — its builder, each reviewer, a re-review, a thread started through a workflow script — gets a one-line description, and its brief names the story by its board id (the `s…` id) in the brief's own text, not only inside a file the brief points to. The board draws a thread on a story from that and nothing else: once a wave ran for three hours with twenty-three of twenty-six threads naming no story, most of them with no description at all, and the owner watching the board saw nothing working.
   **Wait on the result, never on the clock.** A thread's finish reaches you only at your next step, and a `sleep` is a step that cannot end early. Once each wait was one fixed sleep of seven to fourteen minutes, and a thread that finished four seconds into one was not noticed for eight minutes; over the wave that was most of an hour of nothing. Wait in one command that checks every few seconds for the thing you are waiting on — the new commit on the story's branch, the review file, the report — and returns the moment it appears, with a ceiling of a few minutes after which you look at the thread itself. Never a bare `sleep` longer than a minute.
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

Do not run this example unchanged. Keep config/evidence/artifacts outside source worktrees. The helper's
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
bounded coding repair ladder: initial implementation by the lowest of the install's models, then the next model above it, one per step, in order from lowest to highest, with two evaluated repair submissions per tier, six maximum per
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

Cost artifacts are coordinator-owned and separate from the manifest: preserve immutable rate snapshots and deduplicated
response usage, then emit STORY-COSTS.json/CSV/Markdown with source revision/points,
stage/model totals, repair history and explicit completeness/billing limits. Record parent
and child thread IDs for attribution. Link the final report in story delivery evidence;
retain interim snapshots rather than overwriting historical estimates.

## Board transition evidence

The coordinator performs the planning-start and approved-code-review lane moves on the board. They are external operations, not swarm.py commands. Record each attempt
in `board-transitions.jsonl`: story, event, timestamp, project/team/board, actual field and
destination, before/after item revisions, verified outcome or error, and the triggering
planning dispatch or approved base/candidate/report. Keep before/after source snapshots for
successful writes. A failed or queued dispatch is not planning start; a stale or rejected
candidate is not approved code. Already-at-destination is a verified no-op, not another update.

Source checks retain the approved requirement revision in the manifest and reports. Also
record the latest observed board revision. A revision mismatch is acceptable only when a complete
recorded chain from the approved snapshot to that live revision proves exclusively our intended
status/board transitions and their server-generated audit changes, with all requirement-bearing
fields and relations unchanged. Fetch relations when checking this. Do not call reconcile,
replace snapshots, or relabel reviews solely for that proven board-only change. Missing chain,
unexpected edits, or actual source changes still invalidate acceptance and require reconciliation.
The same content comparison may reconcile board-order-only rank changes using a separate
`source-metadata-transitions.jsonl` chain. Verify each intervening revision changed only rank and
server audit metadata; retain snapshots and identify them as observed external changes, not writes
performed by this run. This does not exempt priorities, dependencies, scope, or other source edits.

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

Both commit IDs are full object IDs, not moving branch names. Requirement revision comparisons
use the same integer supplied in config; observed board revisions follow the board-only exception
above. A reconcile source file contains exactly id,
revision, predecessors, ready, and owns. The coordinator attests --stopped only after verifying
no worker is active. Reconciliation validates the full graph/ownership, preserves original
starting base/worktree and dirty edits, and clears candidate/review evidence. An active job
remains blocked until explicitly unblocked. Re-plan against the new approved source; do not
overwrite revisions merely to make stale reviews match.

## feature/scripts/swarm.py

```python
"""Local, serialized manifest/worktree operations. No remote or agent side effects."""
import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class Error(Exception):
    pass


STATES = {'pending', 'starting', 'active', 'candidate', 'reviewed', 'integrated'}


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
    if result.returncode:
        raise Error(result.stderr.strip() or 'Git command failed')
    return result.stdout.strip()


def oid(repo, value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
        raise Error('An exact full commit SHA is required')
    if git(repo, 'rev-parse', '--verify', value + '^{commit}') != value:
        raise Error('Commit SHA did not resolve exactly')
    return value


def ancestor(repo, older, newer):
    result = subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', older, newer],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise Error('Expected starting/review commit is not an ancestor')


def inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if path == root or root not in path.parents:
        raise Error('Worktree must remain strictly inside workspace_root')
    return path


def ownership(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise Error('Ownership must be a nonempty slash-separated relative path')
    if value == '.':
        return value
    if value.startswith('/') or any(x in value for x in '*?[]:'):
        raise Error('Ownership globs and absolute paths are unsupported')
    parts = value.rstrip('/').split('/')
    if any(x in ('', '.', '..') for x in parts):
        raise Error('Ownership path is not normalized')
    return '/'.join(parts).casefold()


def overlap(left, right):
    return left == '.' or right == '.' or left == right or left.startswith(right + '/') or right.startswith(left + '/')


def validate(data):
    if data.get('version', 1) != 1:
        raise Error('Unsupported manifest version')
    for key in ('feature_id', 'repo', 'feature_branch', 'workspace_root', 'stories'):
        if key not in data:
            raise Error('Missing manifest field: ' + key)
    if type(data['feature_id']) is not int or data['feature_id'] <= 0:
        raise Error('feature_id must be a positive integer')
    if not isinstance(data['feature_branch'], str) or not data['feature_branch']:
        raise Error('feature_branch is required')
    if not isinstance(data['stories'], list):
        raise Error('stories must be a list')
    ids = set()
    for story in data['stories']:
        if type(story.get('id')) is not int or story['id'] <= 0 or story['id'] in ids:
            raise Error('Story IDs must be unique positive integers')
        ids.add(story['id'])
        if type(story.get('revision')) is not int or story['revision'] < 1:
            raise Error('Story revision must be a positive integer')
        if type(story.get('ready')) is not bool or not isinstance(story.get('owns'), list) or not story['owns']:
            raise Error('Each story needs boolean ready and nonempty owns')
        story['owns'] = [ownership(x) for x in story['owns']]
        if not isinstance(story.get('predecessors'), list) or any(type(x) is not int for x in story['predecessors']):
            raise Error('predecessors must be integer story IDs')
        if story.get('status', 'pending') not in STATES:
            raise Error('Unknown story state')
    graph = {s['id']: s['predecessors'] for s in data['stories']}
    visiting, visited = set(), set()
    def visit(item):
        if item not in graph:
            raise Error('Missing predecessor: ' + str(item))
        if item in visiting:
            raise Error('Predecessor cycle detected')
        if item in visited:
            return
        visiting.add(item)
        for predecessor in graph[item]:
            visit(predecessor)
        visiting.remove(item)
        visited.add(item)
    for item in graph:
        visit(item)
    return data


@contextmanager
def manifest_lock(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Error(f'Manifest locked: {lock}. Inspect owner before recovering a stale lock.')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'pid': os.getpid()}, stream)
        yield
    finally:
        lock.unlink()


def save(path, data):
    validate(data)
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize(path, config):
    with manifest_lock(path):
        if Path(path).exists():
            raise Error('Manifest already exists; resume using existing manifest')
        data = validate(copy.deepcopy(config))
        data['repo'] = str(Path(data['repo']).resolve())
        data['workspace_root'] = str(Path(data['workspace_root']).resolve())
        data['version'] = 1
        git(data['repo'], 'check-ref-format', '--branch', data['feature_branch'])
        git(data['repo'], 'rev-parse', '--verify', 'refs/heads/' + data['feature_branch'])
        for story in data['stories']:
            if any(k in story for k in ('base_sha', 'candidate_sha', 'worktree', 'review')) or story.get('status', 'pending') != 'pending':
                raise Error('Initial configuration cannot import execution state')
            story['status'] = 'pending'
        save(path, data)
        return data


def queue(data):
    integrated = {s['id'] for s in data['stories'] if s['status'] == 'integrated'}
    reservations = [s for s in data['stories'] if s['status'] not in ('pending', 'integrated')]
    result = []
    for story in sorted(data['stories'], key=lambda s: s['id']):
        if story['status'] != 'pending' or story.get('blocked') or not story['ready'] or not set(story['predecessors']) <= integrated:
            continue
        if any(overlap(a, b) for other in reservations for a in story['owns'] for b in other['owns']):
            continue
        result.append(story['id'])
        reservations.append(story)
    return result


def paths(data, story):
    name = f"feature-{data['feature_id']}-story-{story['id']}"
    return inside(data['workspace_root'], Path(data['workspace_root']) / name), 'swarm/' + name


def verify_tree(data, story, clean=False):
    path, branch = paths(data, story)
    if story.get('worktree') != str(path) or story.get('branch') != branch:
        raise Error('Recorded worktree/branch differs from deterministic assignment')
    if Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path:
        raise Error('Unexpected worktree root')
    common = Path(git(data['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    if Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != common:
        raise Error('Worktree belongs to another repository')
    if git(path, 'symbolic-ref', '--short', 'HEAD') != branch:
        raise Error('Worktree branch changed')
    ancestor(path, oid(path, story['base_sha']), git(path, 'rev-parse', 'HEAD'))
    if clean and git(path, 'status', '--porcelain', '--untracked-files=all'):
        raise Error('Worktree is dirty; preserve and reconcile its changes')
    return path


def current_candidate(data, story):
    path = verify_tree(data, story, clean=True)
    candidate = oid(path, story.get('candidate_sha'))
    if git(path, 'rev-parse', 'HEAD') != candidate:
        raise Error('Candidate is stale; record and review the new commit')
    return path, candidate


def evidence(path):
    raw = Path(path).read_bytes()
    value = json.loads(raw)
    checks = value.get('checks')
    if not isinstance(checks, list) or not checks or any(not isinstance(c, dict) or not c.get('command') or type(c.get('exit_code')) is not int or c['exit_code'] != 0 for c in checks):
        raise Error('Evidence must contain nonempty successful command/exit_code checks')
    return value, {'path': str(Path(path).resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}


def execute(manifest, command, story_id=None, **args):
    with manifest_lock(manifest):
        data = validate(json.loads(Path(manifest).read_text(encoding='utf-8')))
        if command == 'queue':
            return queue(data)
        story = next((s for s in data['stories'] if s['id'] == story_id), None)
        if story is None:
            raise Error('Unknown story')
        repo = data['repo']
        if command == 'start':
            if story.get('blocked') or story['status'] == 'integrated':
                raise Error('Story blocked or already integrated')
            if story['status'] == 'pending':
                if story_id not in queue(data):
                    raise Error('Story is not eligible: dependency, readiness, or ownership')
                path, branch = paths(data, story)
                if path.exists():
                    raise Error('Assigned worktree path already exists')
                if 'refs/heads/' + branch in git(repo, 'for-each-ref', '--format=%(refname)', 'refs/heads/' + branch).splitlines():
                    raise Error('Assigned story branch already exists; refuse to adopt unrelated work')
                base = git(repo, 'rev-parse', '--verify', 'refs/heads/' + data['feature_branch'])
                story.update(status='starting', base_sha=base, worktree=str(path), branch=branch)
                save(manifest, data)  # Persist intent before Git mutation; retry can recover.
            if story['status'] == 'starting' and not Path(story['worktree']).exists():
                Path(data['workspace_root']).mkdir(parents=True, exist_ok=True)
                branches = git(repo, 'for-each-ref', '--format=%(refname)', 'refs/heads/' + story['branch']).splitlines()
                if 'refs/heads/' + story['branch'] in branches:
                    if git(repo, 'rev-parse', story['branch']) != story['base_sha']:
                        raise Error('Existing branch differs from recorded starting commit')
                    git(repo, 'worktree', 'add', story['worktree'], story['branch'])
                else:
                    git(repo, 'worktree', 'add', '-b', story['branch'], story['worktree'], story['base_sha'])
            verify_tree(data, story)
            if story['status'] == 'starting':
                story['status'] = 'active'
        elif command == 'block':
            if story['status'] == 'integrated' or not args.get('reason'):
                raise Error('Cannot block integrated story; reason is required')
            story['blocked'] = args['reason']
        elif command == 'unblock':
            integrated = {s['id'] for s in data['stories'] if s['status'] == 'integrated'}
            if not story['ready'] or not set(story['predecessors']) <= integrated:
                raise Error('Story readiness or dependencies still block resumption')
            story.pop('blocked', None)
        elif command == 'reconcile':
            if not args.get('stopped') or story.get('mutation_worktree') or story['status'] in ('starting', 'integrated'):
                raise Error('Reconciliation requires stopped worker, released mutation tree, and nonintegrated story')
            replacement = json.loads(Path(args['source']).read_text(encoding='utf-8'))
            keys = {'id', 'revision', 'predecessors', 'ready', 'owns'}
            if set(replacement) != keys or replacement['id'] != story_id or type(replacement['revision']) is not int or replacement['revision'] < story['revision']:
                raise Error('Source must contain exact story fields and a nondecreasing revision')
            if story['status'] != 'pending':
                verify_tree(data, story)
            story.update(replacement)
            validate(data)
            if story['status'] != 'pending' and any(overlap(a, b) for other in data['stories']
                if other['id'] != story_id and other['status'] not in ('pending', 'integrated')
                for a in story['owns'] for b in other['owns']):
                raise Error('Updated ownership overlaps another reserved story')
            for key in ('candidate_sha', 'review_base_sha', 'review', 'integration_sha', 'integration_evidence'):
                story.pop(key, None)
            if story['status'] != 'pending':
                story['status'] = 'active'
                story['blocked'] = 'Source reconciled; replan and explicitly unblock before dispatch'
        elif command == 'candidate':
            if story['status'] not in ('active', 'candidate', 'reviewed') or story.get('mutation_worktree') or story.get('blocked'):
                raise Error('Story cannot change candidate in its current state')
            path = verify_tree(data, story, clean=True)
            base, head = oid(repo, args.get('base')), oid(repo, args.get('head'))
            if git(path, 'rev-parse', 'HEAD') != head:
                raise Error('Candidate must equal story branch HEAD')
            ancestor(repo, base, head)
            ancestor(repo, story['base_sha'], base)
            if base != git(repo, 'rev-parse', 'refs/heads/' + data['feature_branch']):
                raise Error('Review base must equal current feature branch tip')
            if base == head:
                raise Error('Candidate has no changes from review base')
            story.update(status='candidate', candidate_sha=head, review_base_sha=base)
            story.pop('review', None)
        elif command == 'review-record':
            if story['status'] not in ('candidate', 'reviewed') or story.get('mutation_worktree') or story.get('blocked'):
                raise Error('Story is not ready for review recording')
            _, head = current_candidate(data, story)
            value, artifact = evidence(args['evidence'])
            pins = dict(base_sha=story['review_base_sha'], candidate_sha=head, story_revision=story['revision'])
            if args.get('base') != pins['base_sha'] or args.get('head') != head or any(value.get(k) != v for k, v in pins.items()):
                raise Error('Review evidence pins differ from current story')
            if value.get('verdict') != 'APPROVED' or value.get('unresolved_findings') != []:
                raise Error('Review has unresolved findings or lacks approval')
            story.update(status='reviewed', review={**pins, 'evidence': artifact})
        elif command == 'mutation-create':
            if story['status'] not in ('candidate', 'reviewed') or story.get('blocked'):
                raise Error('Mutation review requires a candidate')
            _, head = current_candidate(data, story)
            path = inside(data['workspace_root'], Path(data['workspace_root']) / f"mutation-{data['feature_id']}-{story_id}-{head}")
            if story.get('mutation_worktree') not in (None, str(path)):
                raise Error('Another mutation worktree is still assigned')
            if path.exists() and not story.get('mutation_worktree'):
                raise Error('Unowned mutation path exists')
            story['mutation_worktree'] = str(path)
            save(manifest, data)
            if not path.exists():
                git(repo, 'worktree', 'add', '--detach', str(path), head)
            verify_mutation(data, story, clean=False)
        elif command == 'mutation-remove':
            if not story.get('mutation_worktree'):
                return story
            path = inside(data['workspace_root'], story['mutation_worktree'])
            registered = [Path(field[len('worktree '):]).resolve()
                          for field in git(repo, 'worktree', 'list', '--porcelain', '-z').split('\0')
                          if field.startswith('worktree ')]
            if not (story.get('mutation_removing') and not path.exists() and path not in registered):
                path = verify_mutation(data, story, clean=True)
                story['mutation_removing'] = True
                save(manifest, data)
                git(repo, 'worktree', 'remove', str(path))
            story.pop('mutation_worktree')
            story.pop('mutation_removing', None)
        elif command == 'record-integrated':
            if story['status'] not in ('reviewed', 'integrated') or story.get('blocked') or story.get('mutation_worktree'):
                raise Error('Story must be reviewed and mutation worktree released')
            _, head = current_candidate(data, story)
            pins = dict(base_sha=story['review_base_sha'], candidate_sha=head, story_revision=story['revision'])
            if any(story.get('review', {}).get(k) != v for k, v in pins.items()):
                raise Error('Review is stale')
            validated = oid(repo, args.get('validated_head'))
            if validated != head:
                raise Error('Integration must fast-forward to the reviewed candidate exactly')
            if git(repo, 'rev-parse', 'refs/heads/' + data['feature_branch']) != validated:
                raise Error('Feature branch differs from validated integration commit')
            for record in git(repo, 'worktree', 'list', '--porcelain', '-z').split('\0\0'):
                fields = record.split('\0')
                if 'branch refs/heads/' + data['feature_branch'] in fields:
                    feature_path = next(f[len('worktree '):] for f in fields if f.startswith('worktree '))
                    if git(feature_path, 'status', '--porcelain', '--untracked-files=all'):
                        raise Error('Feature checkout is dirty; validation must match the committed candidate')
            ancestor(repo, head, validated)
            value, artifact = evidence(args['evidence'])
            if value.get('validated_head') != validated:
                raise Error('Integration evidence is stale')
            story.update(status='integrated', integration_sha=validated, integration_evidence=artifact)
        else:
            raise Error('Unknown command')
        save(manifest, data)
        return story


def verify_mutation(data, story, clean):
    path = inside(data['workspace_root'], story['mutation_worktree'])
    expected = Path(data['workspace_root']) / f"mutation-{data['feature_id']}-{story['id']}-{story['candidate_sha']}"
    if path != expected.resolve() or Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path:
        raise Error('Unexpected mutation worktree path')
    common = git(data['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != Path(common).resolve():
        raise Error('Mutation worktree belongs to another repository')
    if git(path, 'rev-parse', '--abbrev-ref', 'HEAD') != 'HEAD' or git(path, 'rev-parse', 'HEAD') != story['candidate_sha']:
        raise Error('Mutation worktree is not detached at candidate commit')
    if clean and git(path, 'status', '--porcelain', '--untracked-files=all'):
        raise Error('Mutation worktree is dirty; restore only owned mutations before removal')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init').add_argument('--config', required=True)
    commands.add_parser('queue')
    for name in ('start', 'candidate', 'review-record', 'mutation-create', 'mutation-remove', 'record-integrated', 'block', 'unblock', 'reconcile'):
        command = commands.add_parser(name)
        command.add_argument('story', type=int)
        if name in ('candidate', 'review-record'):
            command.add_argument('--base', required=True)
            command.add_argument('--head', required=True)
        if name in ('review-record', 'record-integrated'):
            command.add_argument('--evidence', required=True)
        if name == 'record-integrated':
            command.add_argument('--validated-head', required=True)
        if name == 'block':
            command.add_argument('--reason', required=True)
        if name == 'reconcile':
            command.add_argument('--source', required=True)
            command.add_argument('--stopped', action='store_true', required=True)
    args = vars(parser.parse_args())
    try:
        if args['command'] == 'init':
            result = initialize(args['manifest'], json.loads(Path(args['config']).read_text(encoding='utf-8')))
        else:
            manifest, command, story = args.pop('manifest'), args.pop('command'), args.pop('story', None)
            result = execute(manifest, command, story, **args)
        print(json.dumps(result, indent=2))
    except (Error, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

## story/SKILL.md

---
name: story
description: Deliver one complete software story against its accepted criteria. Use for implementation of the whole user outcome, not a board read or edit, a bounded fix, or feature-wide delivery.
---

Follow the [skill announcement contract](../task/references/observability.md) for entry, handoff and exit.

Announce `story` before acting. Resolve the accepted criteria from the story's row on the board (`backlog` skill: `board show <id>`) or from material you were handed; a local story is sufficient when there is no row. The story's description carries what it delivers, what "done" means, what it must leave alone, what it waits on, and the **protected behaviours** the owner approved. Freeze the complete criterion IDs, the required checks and the review requirement before implementation. Never silently drop a criterion.

Read the codebase the story lands in before writing. The plan was written as behaviour and may be incomplete or disagree with the code (top document, rule 5). Stop and report through the mind that briefed you, without building past it, only when a question is allowed by the top document, rule 4, or existing behaviour collides with a criterion in a way the plan does not resolve, a gap has more than one defensible answer, or the honest answer widens the story.

Tests: top document, rule 20; the standard is [the test standard](references/test-standard.md). A story whose row says "nothing to protect" gets no new tests; verify it by running it.

Use `task` for separable bounded changes only when useful; completing tasks is not acceptance.

Verify every criterion against the final candidate. Review is required for every story: have an independent thread that did not write the change (`refute-requirements`, `refute-tests`, `refute-edges`) read the pinned candidate against the criteria and protected behaviours, record the revision it approved, and treat that approval as void after any further change. Use the [shared gate](../task/references/completion.md) before claiming acceptance. Report remaining criteria if blocked.

A story ends as a committed, reviewed candidate on its own branch. Never merge it into the main branch, close its board row, or deploy it yourself: integration belongs to `feature`, or to the brief that handed you a single story.

## story/references/test-standard.md

# The test standard

## The standard

A test may only be written when it depends on code external to the test to
pass or fail.

A test must name and guard one specific behaviour against unintentional code
changes. That is only possible when the behaviour is controlled by code
external to the test, and that code is what the test depends on to pass or
fail.

A test must import the code it protects — not copy it, not impersonate it —
and must literally call it.

If that cannot reasonably be done, no test is needed. Say so and move on.
Never substitute a test that asserts something adjacent.

## Applying it

Name the edit that would break the test — a specific change to a specific
live function. If the only edit you can name is "someone deletes this line",
there is nothing external behind the test and it does not get written.

Assert on what the called code produced: the returned value, the stored row,
the file, the request that went out. Not on a value the test computed, a
constant, or a stub's own call log.

Constructing an input is allowed — a dict, a fixed clock, a temp file. What
is asserted on must come from the import.

A stub belongs only where the process stops being ours: the transport, the
clock, the filesystem root. Never on the code the behaviour lives in.

A test that already passes against unchanged HEAD proves nothing about the
change.

Never assert a constant against itself. If the test says fifteen minutes, it
says `15 * 60`.

## Code whose job is to write into production

The doctrine is that existing code is tested by wrapping it: import it, call
it, assert on what it produced. That holds only while calling it is safe.
When the job of the code is to insert something into production (a line in
the live compose file, a key in the live env file, a mind registered on the
live gateway, anything written where the running system reads it), a test
that wraps it performs that insertion every time the suite runs. Such a test
is not written (top document, rule 19).

Do not decide that a redirect, a stand-in or a temporary folder makes it
safe. Until the owner has answered, the behaviour is reported as
unprotected, never covered by a test asserting something adjacent.

Why: a suite once registered three fixture minds on the live gateway, and run
inside another container it would have added a fixture mind to the live stack
file and token lines to the live env file. It redirected some of what the
code writes and not the rest, and which was which depended on where the suite
happened to run.

## Scope

Coverage is judged against the requirement set as a whole, never counted per
requirement. Read all of them, decide whether any test is warranted at all,
and then decide which tests the set needs. There is no quota: a requirement
may carry several tests, one test may guard behaviour several requirements
describe, and a whole change may warrant none.

Plenty of work warrants none. A skill, a document, a configuration value, a
file move, a removal — none of these have code external to a test deciding
whether it passes. Say so and move on.

Where tests are warranted, each one still has to clear the standard above on
its own, and every test name states the behaviour it guards.

Tests land with the code, never as a follow-up.

A removal earns no test. Delete the old tests and stop. Where a removal
leaves real behaviour behind, that behaviour is the requirement and it gets
the test.

A one-time data migration earns no test. Moving data from one store to
another is not a behaviour of the software — nothing runs it again, nothing
regresses it, and there is no future edit a test could catch. What it gets
instead is verification of the run itself: the counts and contents it loaded,
compared against the source, reported per table. That is not a test suite and
it does not live in one. The same holds for a one-off backfill, a schema
cutover and any other job whose whole life is a single execution.

Constraints stated for clarity — "don't break the terminal" — are not
requirements and get no tests. The behaviours they name already have theirs.

Model output is judged by reading it, never by asserting on it.

## Disposal

A test that fails this standard is deleted the moment it is encountered, as
part of the change in hand. Not logged, not proposed, not deferred.

Deletion is per test. Never sweep a suite.

## Gates and oracles

The install's own suite is a gate: zero failures, always. A test referencing deleted code is
deleted; a test reflecting correct new behaviour is patched. No failure is
punted for being unrelated to the work in hand.

A forked upstream's suite is an oracle and is never deleted. Run it on the
files a change touches; triage failures into "ours broke it", "we changed it
deliberately", or "environmental".

## The reviewer

`refute-tests` edits real source to prove which tests pass regardless of
whether the behaviour they name is correct. Lock the working tree before
spawning it and check `git status` after it reports — it leaves mutations
behind.

Kill a finding only when the claim is checked and wrong. What survives gets
the test struck along with its requirement.

## task/SKILL.md

---
name: task
description: Implement one bounded code change or bug fix and verify its result, even when the request references a story. Not for editing board items or delivering a whole story or feature.
---

Follow the [skill announcement contract](references/observability.md) for entry, handoff and exit.

Announce `task` before acting. Own only the requested behaviour. Route a board lookup or edit to `backlog`, and whole-story acceptance to `story`, before writing. Do not require a board item, create a branch, or enlist another thread just because this is a task.

Identify the observable result and the required verification before editing. For a bug, establish the failure where practical; implement the fix and verify the affected behaviour. Keep expected checks even if they cannot run. Before reporting completion use the [shared gate](references/completion.md). Report the outcome and any verification gap. Do not close a parent item.

A passing gate does not authorise a push, a pull request, a board change or a deployment. Do those only when the brief you were given says to.

## task/references/completion.md

# Delivery gate

Run `python <task-skill>/scripts/gate.py task|story|feature` with one JSON object on stdin (or give its path as the second argument). Use a pipe for a small task; no permanent manifest required. For a story/feature, derive the required IDs and checks from the previously agreed scope, not just the checks that passed. Preserve evidence in the story artifacts/feature manifest. A failed, skipped, stale or missing check is not done. A gate pass checks structure and reported evidence, not the truth of a free-text assertion. Verify the actual output, revision and target first. Never infer authorization from a pass.

All routes require `checks` (nonempty unique list of expected IDs), `passed` (map from passed check IDs to concrete evidence), and `result` (observed outcome). Story also requires `criteria` (expected IDs), `accepted` (map from satisfied IDs to evidence), `candidate` (current revision), and boolean `review_required`; when true, `approved_revision` must equal `candidate` after an actual approved review. Feature requires `stories` (ordered IDs) and `integrated` (map from accepted and integrated IDs to evidence identifying the candidate and verified target). Use string IDs. Before reporting done or invoking an authorized closing helper, run the gate on current evidence; on nonzero exit do not perform the action. This gate writes nothing to the board.

Example task input: `{"result":"Typo corrected in the rendered heading","checks":["heading"],"passed":{"heading":"Focused render check passed on the edited checkout"}}`.

## task/references/observability.md

# Skill observability

When you actually begin using any skill (including a helper, a delegated skill or a later re-entry), announce it in user-visible commentary before its first action. Use one standalone line per invocation, in this exact format:

Skill: `skill-name` — short reason this skill owns the requested outcome.

Use the invoked skill name. Never announce a catalogue listing, a file inspection, an example, or mere consideration as an invocation. Do not announce again for each step or for returning from a helper. If routing changes, announce the new skill with the reason. When a running skill finishes, emit one standalone commentary line `Skill end: ` followed by its backtick-quoted name, a space, and `done`, `blocked`, `unverified`, `failed`, or `rerouted`. Use the same name as at start. Do not include sensitive identifiers or prompt excerpts. Announcements are observational: they never authorise work or replace evidence.

For code delivery, prefer the smallest matching route: `task` for a bounded change, `story` for acceptance of one complete story, `feature` for an entire feature or an ordered list of stories. A read or edit of a board item alone is not code delivery.

## task/scripts/gate.py

```python
import argparse
import json
from pathlib import Path
import sys


def text(value):
    return isinstance(value, str) and bool(value.strip())


def missing(scope, record):
    if not isinstance(record, dict):
        return ['expected an evidence object']
    errors = [] if text(record.get('result')) else ['missing observed result']
    groups = [('checks', 'passed')]
    if scope == 'story':
        groups.append(('criteria', 'accepted'))
        if not text(record.get('candidate')):
            errors.append('missing candidate revision')
        if type(record.get('review_required')) is not bool:
            errors.append('missing review decision')
        elif record['review_required'] and record.get('approved_revision') != record.get('candidate'):
            errors.append('current candidate lacks approved review')
    if scope == 'feature':
        groups.append(('stories', 'integrated'))
    for expected, observed in groups:
        ids = record.get(expected)
        if not isinstance(ids, list) or not ids or not all(text(item) for item in ids) or len(set(ids)) != len(ids):
            errors.append(f'{expected}: need nonempty unique string IDs')
            continue
        evidence = record.get(observed, {})
        if not isinstance(evidence, dict):
            errors.append(f'{observed}: expected an evidence map')
            continue
        for item in ids:
            if not text(evidence.get(item)):
                errors.append(f'{observed}: missing evidence for {item}')
        if evidence.keys() - set(ids):
            errors.append(f'{observed}: evidence outside agreed scope')
    return errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Check reported delivery evidence; never modifies external state.')
    parser.add_argument('scope', choices=['task', 'story', 'feature'])
    parser.add_argument('file', type=Path, nargs='?')
    args = parser.parse_args()
    try:
        record = json.loads(args.file.read_text(encoding='utf-8-sig')) if args.file else json.load(sys.stdin)
        errors = missing(args.scope, record)
    except (ValueError, OSError) as exc:
        errors = [str(exc)]
    print(json.dumps({'eligible': not errors, 'missing': errors}))
    sys.exit(int(bool(errors)))
```

## story/agents/refute-requirements.md

---
name: refute-requirements
description: Adversarial reviewer. Given a numbered requirement list and a diff, tries to prove the implementation does not actually satisfy the requirements.
---

You are trying to prove this implementation wrong. You did not write it and
you owe it nothing.

You are given a numbered requirement list and the diff that claims to
satisfy it. For each requirement, find the path where the code appears to
satisfy it but does not: the case the author did not run, the branch that
returns early, the state where the observable outcome differs from what the
requirement states.

Read the actual code. Do not reason from the diff summary or from what the
requirement says the code should do.

Return one finding per requirement you can break, each with the requirement
number, the concrete input or state that breaks it, and what happens
instead. If a requirement genuinely holds, say so in one line and move on —
do not pad. Say plainly when you could not break any of them.

## story/agents/refute-edges.md

---
name: refute-edges
description: Adversarial reviewer. Hunts the inputs, empty states, failure paths and concurrent access that break an implementation.
---

Find what breaks this that nobody thought about.

Work the boundaries: empty input, absent file, unreadable permission, the
value that is zero rather than missing, the unicode string, the path that
already exists, the second caller arriving mid-write, the network call that
hangs rather than fails, the partial write interrupted halfway.

Prefer failures that are silent over failures that are loud — a crash is
visible and gets fixed, a wrong answer that looks right is the one that
ships.

Report each as the concrete input or sequence, the resulting behaviour, and
whether it is silent. Rank by whether the owner would notice. Say plainly when
you found nothing worth the owner's attention rather than inventing something.

## story/agents/refute-tests.md

---
name: refute-tests
description: Adversarial reviewer. Given tests and the code they cover, proves which tests pass regardless of whether the behaviour they name is correct.
---

A test that passes against a broken implementation proves nothing. Find
those.

For each test, break the behaviour it claims to prove — invert a condition,
return a constant, delete the write — run that single test, and see whether
it fails.

## Restoring the tree is part of the job, not a courtesy

You edit real source files that other people and other agents are reading
while you work. A mutation left behind is indistinguishable from a genuine
regression, it survives a session rotation, and the next session debugs a bug
nobody wrote. This has happened more than once. Treat it as the failure mode
that matters most — a review that finds nothing and leaves the tree clean is
worth more than one that finds everything and leaves a mutant.

Before the first mutation, copy every file you intend to touch to a unique
temporary directory. That copy is the restore source.

**Never `git checkout`, `git stash`, or `git restore` these files.** The code
under review is usually uncommitted — it was written minutes ago — so those
commands do not undo your mutation, they delete the entire feature.

Restore after *each* mutation, before starting the next. Not in a batch at the
end: you may run out of context, hit an error, or be stopped mid-run, and
whatever is on disk at that moment is what everyone else inherits. One
mutation live at a time also keeps a concurrent reviewer from reporting your
mutant as a real defect.

When you are done, diff every touched file against its temporary copy and
confirm they match. Open your report by stating that you did this and what it
showed. If a file does *not* match, say so first and loudly, quoting the
difference — a restore you failed to complete is the single most important
thing in your report.

Report every test that survived a break it should have caught, naming what
you broke and why the test missed it. Also flag any test that asserts
something no numbered requirement states, and any requirement with no test
that genuinely covers it.

Do not rewrite the tests. Report.
