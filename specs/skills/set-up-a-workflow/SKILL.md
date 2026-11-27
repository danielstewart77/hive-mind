---
name: set-up-a-workflow
description: Set up a workflow on this install from its documents — ask which models serve which tier, write each role's skills, add them to the roles, and keep an untouched as-generated copy; also update those skills when a document changes. Trigger when the owner asks to set up, install, or update a workflow, or to try one in a test folder.
---

# /set-up-a-workflow

A stage document is a tuned skill's own text with what belongs to one
install taken out, so writing a skill from it is mostly putting this
install's specifics back. The workflow is Chief of Staff. Its documents are
in `docs/workflows/chief-of-staff/` in the published code you carry
(`$HIVE_PROJECT_DIR`, else your working directory; if you cannot find them,
ask the owner where they are). `roles` below is
`python3 "$HIVE_PROJECT_DIR/tools/stateless/roles/roles.py"`; it uses your own key
(`MIND_KEY`) and `HIVE_SITE_URL`, both in your environment. Never print
a credential and never put one on a command line. When unsure of a command's
shape, run it with `--help` and use the shape it shows exactly.

    roles list | show ROLE | catalog
    roles create NAME [--slug S] [--suggested-tier T] [--skills a,b] [--tools ..] [--hooks ..]
    roles update ROLE [--name N] [--suggested-tier T | --clear-suggested-tier] [--skills a,b]
    roles add-skill ROLE SKILL | remove-skill ROLE SKILL
    roles keep ROLE | promote ROLE | take SLUG

`update --skills` replaces the whole list; use `add-skill` to give one skill.

## Test mode

If you are told to write into a test folder, touch no role: no `roles` call,
no write into the install folder, no write of the install file. Steps 4 and 5
and the install file all go under that folder, laid out like the install
folder: skills at `<folder>/<skill>/`, copies at `<folder>/.as-generated/`,
the install file at `<folder>/.install/chief-of-staff.md`. Each test run
writes to a new, empty folder; refuse one that is not empty.

## 1. Read

Read the top document, then every stage document beside it.

## 2. Roles

Run `roles list`. Create each role the documents name that is missing, with
`roles create`, including the three lens roles the review document calls
for, one each for the requirements, tests and edges lenses: a stage cannot be
given a skill or a holder before its role exists. You create roles; the
owner gives minds their roles.

## 3. Ask, and record

Set each role's suggested tier with `roles update ROLE --suggested-tier T`
(`low`, `medium`, `high` or `extra-high`), using the tier the top document
gives that role. A role names no model and decides none: the tier is shown
to the owner above the model box of a mind holding the role, and the owner
sets each mind's model on the mind. Ask the owner which of this install's
models serves each tier, extra high, high, medium and low, and record it
with the other answers. Then ask everything the
documents say to ask: the deployment document above all, and each role's
needs as the top document lists them. Record the answers in the install's own
file, `install/workflow-skills/.install/chief-of-staff.md`, in the four
sections the top document names, every line dated. Record the slugs of the three lens roles in its Routing preferences.
Write it atomically (write
a dot-named file beside it, then rename it into place). Never record who
holds a role: that is asked of the gateway when needed.

## 4. Write each role's skills

For each skill a stage document names, in the file's own order: write the
document's text for that skill, with this install's specifics put back from
the install file. Each skill carries, near its top, the declaration the top
document gives, in its exact wording, and carries its sub-mind briefs and
helper files inside its own folder. A mind runs its skill, never the top
document, and a sub-mind brief or a lens never sees it: so wherever the
document says "top document, rule N", write the full text of that rule from
the top document into the skill there, and into each sub-mind brief and lens
that the rule binds (a rule that binds every brief, such as never printing a
key, goes into every brief). Leave no pointer to the top document in any
skill. Write in the harness-neutral reference
format, as the skills in `specs/skills/` are: `name` and `description`
frontmatter, and never the name of a harness's own spawning tool.

Write each skill whole in a scratch place inside your own folder. Then copy
it into a dot-named staging name inside `install/workflow-skills/` and rename
it to `install/workflow-skills/<skill>/` only if that name does not exist, so
no mind is handed half a skill. A skill that exists is not written again: see
step 7. Give it to its role last (step 6).

## 5. The as-generated copy

When the skill is final, copy it to
`install/workflow-skills/.as-generated/<skill>/` (staged and renamed the same
way), and add one file to that copy, `.generated.json`, with these keys:

    {"workflow": "chief-of-staff",
     "document": "<stage document, relative to the checkout root>",
     "document_version": "<sha256 hex of that document's bytes>",
     "top_document": "<top document, relative to the checkout root>",
     "top_version": "<sha256 hex of the top document's bytes>",
     "document_commit": "<commit that last changed the stage document>",
     "top_commit": "<commit that last changed the top document>"}

Compute each version with `sha256sum <file>` and each commit with
`git log -1 --format=%H -- <file>`. Paths are relative to the checkout root,
never absolute, never with `..`. The copy is never tuned: it stays as the
skill was generated.

## 6. Give the skills to the roles

Add each skill to its role with `roles add-skill`. Mark each role you changed
with `roles keep ROLE`, so a later pull leaves it alone.

## 7. Updating

When a stage document has been updated, read what changed: the recorded
`document_commit` names the text the skill was written from, so run
`git diff <document_commit> -- <document>` (and the same for the top
document with `top_commit`). Apply that change to the existing skill. Never
regenerate a skill that exists: wording it afresh would discard this
install's tuning and bury the real change. Then apply the same change to the
as-generated copy, never copying the skill over it, so the copy stays
untuned, and record the new versions and commits.

## Safety

Never regenerate an existing skill. Never write into the tracked
`specs/skills/` folder, in any mode. Never print a credential. Where a
document says to ask, ask the owner; do not assume the answer.

## An install that predates this skill

Roles are seeded once; a pull does not add this skill to the operator role.
A person adds this skill to the operator role once on the Roles page.
