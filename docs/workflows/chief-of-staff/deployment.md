Stage document for the operator role; it is the text of the skills below with this install's specifics removed.

## deploy-a-change/SKILL.md

---
name: deploy-a-change
description: Ship a reviewed change to merged, pulled, restarted and verified in use.
---

## The board

Work through a feature's stories in waves, one wave in progress at a time (top document, rule 15).

A feature is done when every one of its stories is `done`, or back in `committed` with its reason, or on the list of ones only the owner can settle. Reporting a feature's state while one of its stories sits untouched and unexplained is how a feature stays unfinished for a week.

## 5. Verify and ship

Walk the numbered requirement list and verify each one against the test that guards it, or — where no test could reach it — by running the thing and looking at it. A requirement you cannot demonstrate is not shipped. Then walk the approved protected behaviours and confirm each is either covered by a test that would fail if it broke, or reported as unprotected.

Then pull. Report what shipped (top document, rule 21). A requirement that appeared after the read-back and was never mentioned again is a feature the owner did not know they were getting.

## What the operator asks the owner

How does a change ship on this install: merge and restart containers, a pull request and a deployment to a server, a push straight to a release branch, or something else? Which checks run before and after? What is the exact procedure, step by step: how to confirm the system is quiet, how to restart, how to verify the change is in use, how to roll back? Who may approve? The answers are written into the generated deployment skills and into the install file.
