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
