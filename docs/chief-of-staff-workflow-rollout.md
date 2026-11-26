# Rolling the Chief of Staff workflow out to another install

## The idea

What ships is a **workflow**: one package that an install's operator can read
and set up from. The Chief of Staff workflow is the first. A workflow says
which minds it needs and with what settings ("to run this you need three
more minds: a planner on your big model, a coder, a reviewer"), and carries
the skills those minds run. An install that has only an operator can be
handed the workflow; the operator tells Daniel what is missing, he creates
the minds, and each gets its role and skills from the package.

A workflow is also the unit of change. Reworking code review at work into
three reviewers and a judge is an update to the workflow and the skills that
travel with it. The contract: a change is published by updating the workflow
package itself; a change that never reaches the package stays local to the
install that made it.

## Where things stand (2026-11-23)

- The console already has roles that hand a mind its skills
  (`hive/src/roles/*.yaml`: chief-of-staff, developer, general, operator,
  optimizer, planner), and hive-mind already ships tracked skills under
  `specs/skills/`.
- The skills that made the workflow work are mostly not tracked. The coder's
  `feature`, `story` and `task`, the planner's `plan-a-task` and the
  reviewer's `review-code` live only in their minds' own folders. The chief
  of staff's `take-a-task` and `tend-the-ledger` are tracked but the copies
  Stash runs have drifted from them.
- There is no coder or reviewer role in the shape the workflow uses.

## Decisions

- **A stage document is the skills' own text with the install's specifics
  removed: DECIDED** (Daniel, 2026-11-24): "these shouldn't be bigger than the skills
  themselves." Made by taking the tuned skill's text and removing lines,
  never by describing the skill in new words; one short top document and
  the setup skill carry what no skill states.
- **Where a generated skill lives: DECIDED** (Daniel, 2026-11-23: "Yes, every mind
  and the workflow will run on the same machine."). One shared folder on that
  machine, ignored by git so a pull never touches it, attached read-only to
  each mind and writable to the operator's mind. The operator is a mind
  created in the console, and the operator may do anything to a role.
- **Home-specific content comes out of a skill before it ships** (Daniel,
  2026-11-23). A shipped skill holds only instructions true on any install.
  What belongs to one install — the list of its minds, their models and
  plans, standing orders such as "switch Cypher to Sonnet when her plan runs
  out" — lives in a separate file on that install that a pull never
  overwrites. Reason: at work the minds, models and plans are different, and
  a chief of staff told to brief a mind that does not exist there is broken
  on arrival.
- **The work install runs the same spread of models, none of them Claude**
  (Daniel, 2026-11-23): a big model for the planner, smaller ones below it,
  the same paradigm as home. Consequence: which model a role runs on is a
  fact about the install, not about the role. The shipped role files name
  no model, so the model is chosen on each install rather than shipped.
  Since 2026-11-26 (feature 893) no role decides a model at all: a mind runs
  the model it is configured with and a conversation asked for a model gets
  it, whatever its roles name.
- **Skills may be tuned in place on an install; publishing is a separate,
  deliberate act** (Daniel, 2026-11-23). The fast loop that made the first
  run work - a mind misbehaves, its skill is fixed, the next turn has the
  fix - stays. The other install sees the change only when the workflow
  package is updated.
- **A workflow is a specification that skills are generated from, not a
  bundle of skill files** (Daniel, 2026-11-23). The package holds no
  `SKILL.md`. (REVISED 2026-11-24: it holds the skills' own text with the
  install's specifics removed; no state diagram, no full paragraphs
  added.) A recommended model for each mind is given as a tier - extra high, high, medium, low - never a named
  model. An install's operator is handed the document and told "I have
  created these five minds, put these skills on them", and writes each
  mind's skills from it. Reasons: a copied skill is not the skill the mind
  that tuned it is running; unwinding one install's names from raw files is
  worse than writing fresh; and a mind does this work, nobody drags files.
- **How a workflow stays current** (Daniel, 2026-11-23), two parts. First,
  a skill that belongs to a workflow says so, and says that whoever changes
  it asks Daniel whether to document the change in the workflow. Second,
  when the operator generates a skill it writes it twice: the copy the mind
  runs, and an untouched copy of exactly what was generated, kept beside
  it. Generated skills are tracked by nothing, so this is the install doing
  its own versioning: the difference between the two files is precisely
  the tuning done on that install since generation. The operator can list
  at any time which skills have drifted, draft the matching change to the
  workflow document for each, and bring it to Daniel to approve or discard.
- **A hierarchy of documents, not one** (Daniel and Skippy, 2026-11-23).
  One short document is the workflow itself: the stages, their order, who
  hands what to whom, as a diagram. Each stage - taking the task, planning,
  building, review, deployment - is its own document with its own diagram
  and its own details, and is what one role's skills are generated from.
  Reason: the first thing to change after shipping will be review (Daniel
  intends to have his planner at work produce several adversarial review
  designs and keep the best), and that should replace one document and
  move nothing else.
- **REVISED 2026-11-24: no contract section in a stage document; the
  hand-offs are the one diagram in the top document.** Was: each stage
  document states its contract with its neighbours (Skippy):
  what it is handed, what it hands back and to whom, in what form. A stage
  can be swapped for any design that keeps the contract; that is the only
  thing that makes the pieces independent. Review, for example: handed an
  open unmerged pull request and the feature's approved requirements;
  hands back graded findings to the chief of staff.
- **Rules every role follows live once, in the top document** (Skippy), and
  every generated skill carries them - for example that an approved
  requirement is the answer and is never re-asked. Stated per stage they
  would drift apart.
- **What a role's mind needs besides the skill is listed in the top
  document** (Skippy; moved there from the stage documents 2026-11-24):
  model tier, tools, and credentials with the access they need.
  On 23 November the coder could not finish because her GitHub token could
  write to one repository of the three the feature touched.
- **A workflow is proven at home before it ships** (Daniel, 2026-11-23:
  worth the tokens to save trouble at work). The thing being simulated is
  one operator writing skills from the document, so the test is exactly
  that and no more: one mind, in a fresh session that has never seen the
  real skills, is handed the document and told to write the skill for the
  planner, the coder, the reviewer, the chief of staff and itself, into a
  test folder, not into anybody's skills. The tuned skills are the gold
  standard. Skippy then compares the two, rule by rule: every rule in a
  tuned skill that the generated one lacks or weakens is a hole in the
  document, fixed there, and the test is run again from a fresh session.
  No spare minds, no feature run.
- **An updated workflow is applied to existing skills as a change, never by
  regenerating them** (Skippy). Generation words things differently every
  time, so regenerating would discard an install's tuning and bury the real
  change under rewording. The operator reads what changed in the document
  and makes that change in the skill, then refreshes the as-generated copy.
- **At home the tuned skills are the origin.** Their as-generated copy is
  their text on the day the document is written from them (Skippy).
- **The drift check is a file comparison, not a mind reading skills**
  (Skippy). A small script walks every generated skill on the install and
  compares the file the mind runs with the as-generated copy beside it. It
  costs no tokens and prints nothing when nothing differs. Only when it
  finds a difference is a mind involved: it reads that difference, drafts
  the matching change to the stage document, and brings it to Daniel.
- **When the drift check runs** (Daniel, 2026-11-23): on request. It is a
  script and nothing more for now. No nightly run, nothing attached to
  Optimo, and no instruction about it in any skill or stage document; it
  gets hooked into something later.
- **The as-generated copy records which version of its stage document it
  was written from** (Skippy). The same script then reports the opposite
  drift too: a stage document that has changed since an install's skills
  were generated from it.
- **Deployment is asked, not prescribed** (Daniel, 2026-11-23). How work
  ships varies too much to write down once: sometimes merge and restart
  containers as at home, sometimes a pull request and a deployment to an
  IIS machine, sometimes a push straight to a release branch. The
  deployment stage document therefore says what has to be asked - how this
  ships, which checks run, what the procedure is - and the operator asks
  rather than assumes. In the home test Skippy answers those questions as
  for the home environment and the generated skill is judged on that.
- **Skills are generated for roles, not for minds** (Daniel, 2026-11-23).
  The operator needs no other mind to exist. Its instruction is: write
  these skills for these roles and add them to the roles. Daniel then
  creates minds in the console and assigns each a role, and the role gives
  the mind its skills. A role gives and never restricts (decided
  2026-11-19): a mind runs any skill that exists for it.
- **Consequences of generating for roles** (Skippy). The as-generated copy
  and the drift check are per role, one pair for each skill, not one per
  mind. A skill is tuned in the role's copy, so every mind holding the
  role gets the fix. And a skill written before any mind exists cannot
  name minds: the chief of staff finds out who holds the coder role by
  asking the install, not by reading a list in its skill.
- **Publishing is ordinary git** (Daniel, 2026-11-23). A changed workflow
  document is committed and pushed to the repository like anything else,
  from whichever install changed it. Nothing special is designed for it.
- **The tracked skills folder holds only skills that are identical on
  every install** (Daniel, 2026-11-23). A skill that is personalised, or
  that a workflow document generates, is not tracked: the two sources
  conflict and the stale one returns on every pull. `take-a-task` and
  `tend-the-ledger` are removed. Skippy's first pass over the fifteen in
  `specs/skills`, by description and a search for house names, to be
  confirmed by reading each before anything is deleted: also removed are
  `plan-feature` (the planning stage generates it) and `inbox-profile`
  (one mind's personal skill); kept verbatim are the memory and session
  skills (`always-remember`, `remember`, `memory`, `save-session`,
  `end-session`, `rotate-session`, `self-reflect`,
  `review-identity-proposal`) and the three every role leans on
  (`backlog`, `brief-another-mind`, `brainstorm`), with Daniel's name
  taken out of the ones that carry it. The role files stop naming what is
  removed. The removal lands in the same change that ships the documents,
  not before, so no pull delivers a role naming a skill that is gone.
- **The operator is the starting point of an install** (Daniel,
  2026-11-23). The operator role ships with tracked, verbatim skills, one
  of which is setting up a workflow: read the workflow documents, write
  the skills for each role, add them to the roles, keep the as-generated
  copy, ask what the documents say to ask. An install with nothing but an
  operator unpacks the rest from there. That skill is itself what the
  home test exercises: the fresh session is given it and the documents
  and nothing else (Skippy).
- **Repository consolidation is not a prerequisite.** The blocker is that
  the skills are untracked, not how many repositories there are.

## Non-goals

- Regenerating the home install's skills. Home keeps the tuned skills its
  minds run; generation is for a new install (Daniel, 2026-11-23).
- Cleaning up, de-duplicating or removing the skills individual minds
  hold. A separate effort (Daniel, 2026-11-23). The tracked folder is in
  scope; see Decisions.

## Open questions

None.


## Risks

- Tuning on an install that never opens a pull request against the
  workflow documents is found only when someone asks for the check.

## Prior art
