---
name: create-story
description: Create a Planka story card with proper structure. Use when Daniel wants to create a new story/task on the board. ALWAYS use this skill — never create a card inline.
---

# Create Story

Creates a well-structured Planka card. A card with insufficient detail is worse than no card — it wastes triage time and produces bad implementation plans. This skill enforces the minimum bar.

## Step 1 — Gather Required Information

Before creating anything, you must have all of the following. If any are missing, ask.

**Required from context or Daniel:**
- **Title** — `[Category] Short imperative description` (e.g. `[Bug] Fix pydantic_core load failure on host`)
- **Category** — Bug | Feature | DevOps | Security | Refactor | Docs | Memory | Multi-Mind
- **Problem statement** — What is broken or missing? Be specific. Include error messages, file paths, symptoms.
- **Root cause** (if known) — Why is it happening? If unknown, say so explicitly.
- **Affected scope** — Which files, tests, endpoints, or components are involved?
- **Fix approach** — What is the intended solution? At minimum a direction; ideally specific steps.
- **Acceptance criteria** — Concrete, checkboxed, verifiable. Minimum 3 items. Each must be independently testable.
- **Dependencies** — Any cards that must merge first? Any external requirements?
- **Target list** — Backlog (default) or In Progress

**Do not invent information.** If root cause is unknown, write "Unknown — needs investigation." If fix approach is uncertain, write "TBD — see acceptance criteria for constraints."

## Step 2 — Draft the Description

Use this template exactly. Do not omit sections.

```markdown
## Problem

<1-3 paragraph description of what is wrong or what is needed. Include error messages verbatim if applicable. Include file paths. Be specific enough that a developer with no context can understand the issue.>

## Root Cause

<Known cause, or "Unknown — needs investigation.">

## Affected Scope

<List of files, test files, endpoints, components, or systems impacted.>

## Fix Approach

<Intended solution. Can be high-level direction or specific steps. If multiple options exist, list them with tradeoffs.>

## Acceptance Criteria

- [ ] <specific, independently verifiable condition>
- [ ] <specific, independently verifiable condition>
- [ ] <specific, independently verifiable condition>
<add more as needed — minimum 3>

## Dependencies

<Other cards that must merge first, or "None.">

## Notes

<Any additional context, constraints, or gotchas. Delete section if empty.>
```

## Step 3 — Create the Card

Use the Planka tool:

```bash
PLANKA_EMAIL=mind@example.com PLANKA_PASSWORD='J35u5*k!#g' \
  python3 /usr/src/app/tools/stateless/planka/planka.py create-card \
  --list-id <list-id> \
  --name "[Category] Title" \
  --description "<full description from Step 2>" \
  --card-type story
```

List IDs:
- Backlog: `1720152629494940682`
- In Progress: `1720153348214096907`

## Step 4 — Assign Ada Label

Get a token first:
```bash
TOKEN=$(curl -s -X POST http://planka:1337/api/access-tokens \
  -H "Content-Type: application/json" \
  -d '{"emailOrUsername":"mind@example.com","password":"J35u5*k!#g"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['item'])")
```

Then assign:
```bash
curl -s -X POST "http://planka:1337/api/cards/<card-id>/card-labels" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"labelId":"1720207192893686912"}'
```

Ada label ID: `1720207192893686912`

Only assign Ada label if this card is Ada's work. Skip if it is Daniel's.

## Step 5 — Confirm

Output:
```
Created: [Category] Title
Card ID: <id>
List: Backlog | In Progress
Ada label: assigned | skipped
```

## Quality Bar

Before confirming, check:
- [ ] Problem section contains specific error messages or symptoms (not just "tests fail")
- [ ] Acceptance criteria are checkboxes with verifiable outcomes (not "it works")
- [ ] Affected scope names actual files or test files
- [ ] Root cause says something concrete or explicitly says "Unknown"
- [ ] Fix approach gives direction, not just "fix it"

If any check fails, revise before creating.
