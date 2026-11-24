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
`python3 "$HIVE_PROJECT_DIR/tools/stateless/roles/roles.py"`; its credentials,
`HIVE_SITE_URL` and `ROLES_PUSH_TOKEN`, are in your environment. Never print
a credential and never put one on a command line. When unsure of a command's
shape, run it with `--help` and use the shape it shows exactly.

    roles list | show ROLE | catalog
    roles create NAME [--slug S] [--model M] [--skills a,b] [--tools ..] [--hooks ..]
    roles update ROLE [--name N] [--model M | --clear-model] [--skills a,b]
    roles add-skill ROLE SKILL | remove-skill ROLE SKILL
    roles keep ROLE | promote ROLE | take SLUG

`update --skills` replaces the whole list; use `add-skill` to give one skill.

## Test mode

If you are told to write into a test folder, touch no role: no `roles` call,
no write into the install folder, no write of the install file. Steps 3 and 4
and the install file all go under that folder, laid out like the install
folder: skills at `<folder>/<skill>/`, copies at `<folder>/.as-generated/`,
the install file at `<folder>/.install/chief-of-staff.md`. Each test run
writes to a new, empty folder; refuse one that is not empty.

## 1. Read

Read the top document, then every stage document beside it.

## 2. Ask, and record

Ask the owner which of this install's models serves each tier: extra high,
high, medium, low. Set it on each role with `roles update ROLE --model M`,
using the tier the top document gives that role. Then ask everything the
documents say to ask: the deployment document above all, and each role's
needs as the top document lists them. Record the answers in the install's own
file, `install/workflow-skills/.install/chief-of-staff.md`, in the four
sections the top document names, every line dated. Write it atomically (write
a dot-named file beside it, then rename it into place). Never record who
holds a role: that is asked of the gateway when needed.

## 3. Write each role's skills

For each skill a stage document names, in the file's own order: write the
document's text for that skill, with this install's specifics put back from
the install file. Each skill carries, near its top, the declaration the top
document gives, in its exact wording, and carries its sub-mind briefs and
helper files inside its own folder. Write in the harness-neutral reference
format, as the skills in `specs/skills/` are: `name` and `description`
frontmatter, and never the name of a harness's own spawning tool.

Write each skill whole in a scratch place inside your own folder. Then copy
it into a dot-named staging name inside `install/workflow-skills/` and rename
it to `install/workflow-skills/<skill>/` only if that name does not exist, so
no mind is handed half a skill. A skill that exists is not written again: see
step 6. Add it to its role last.

## 4. The as-generated copy

When the skill is final, copy it byte for byte to
`install/workflow-skills/.as-generated/<skill>/` (staged and renamed the same
way), and add one file to that copy, `.generated.json`, with exactly these
five keys:

    {"workflow": "chief-of-staff",
     "document": "<stage document, relative to the checkout root>",
     "document_version": "<sha256 hex of that document's bytes>",
     "top_document": "<top document, relative to the checkout root>",
     "top_version": "<sha256 hex of the top document's bytes>"}

Compute each version with `sha256sum <file>`. Paths are relative to the
checkout root, never absolute, never with `..`.

## 5. Roles

Create a role that is missing with `roles create`. Add each skill to its role
with `roles add-skill`, never `update --skills`, so no other grant is lost.
Mark each role you changed with `roles keep ROLE`. Never `take` (it replaces
the role's skills with the shipped list and drops every generated name) and
never `promote` (it writes this install's names into the tracked file).
Review needs three lens holders: create the three lens roles the review
document names (you create roles; the owner gives minds their roles) and
record their slugs in the install file's Routing preferences.

## 6. Updating

When a stage document has been updated, read what changed: a diff of the
recorded `document_version` against the document now (and the top document
the same way). Apply that change to the existing skill. Never regenerate a
skill that exists: wording it afresh would discard this install's tuning and
bury the real change. Then refresh the as-generated copy and its record: the
skill's files as they are now, and the new versions.

## Safety

Never regenerate an existing skill. Never write into the tracked
`specs/skills/` folder, in any mode. Never print a credential. Where a
document says to ask, ask the owner; do not assume the answer.

## An install that predates this skill

Roles are seeded once; a pull does not add this skill to the operator role.
A person takes the shipped operator role once on the Roles page to receive
it.
