Stage document for the operator role; it is the text of the skills below with this install's specifics removed.

## deploy-a-change/SKILL.md

---
name: deploy-a-change
description: Ship a reviewed change to merged, pulled, restarted and verified in use.
---

## The board

The story's lane is how the owner follows it, so move it as the work moves (top document, rule 15). Work through a feature's stories in waves, one wave in progress at a time.

A feature is done when every one of its stories is `done`, or back in `committed` with its reason, or on the list of ones only the owner can settle. Reporting a feature's state while one of its stories sits untouched and unexplained is how a feature stays unfinished for a week.

## 5. Verify and ship

Walk the numbered requirement list and verify each one against the test that guards it, or — where no test could reach it — by running the thing and looking at it. A requirement you cannot demonstrate is not shipped. Then walk the approved protected behaviours and confirm each is either covered by a test that would fail if it broke, or reported as unprotected.

Then pull. Report what shipped (top document, rule 21). A requirement that appeared after the read-back and was never mentioned again is a feature the owner did not know they were getting.

Then clean up the worktree (top document, rule 18). Once the work is merged and pulled, the worktree it was built in goes, and its branch with it, on this machine and on the remote: `git worktree remove <path>`, `git branch -d <branch>`, `git push origin --delete <branch>`, then `git worktree prune`. This is a step of shipping, not a tidy-up for later: two stale worktrees once still held tests that registered fixture minds on the live gateway, after the fix had merged everywhere else. Use the plain forms. A refusal means the worktree holds uncommitted changes or its branch holds commits main does not; that is work nobody merged, so say what is there rather than forcing it away.

## restart-a-service/SKILL.md

---
name: restart-a-service
description: >
  Restart a service that carries this conversation without cutting the owner off mid-sentence. Trigger on any request to restart, bounce, reload or redeploy one of those, and on any deploy whose last step is one of those restarts. The rule is: say almost nothing, arm a generous timer, then stop talking.
---

## The failure this exists to prevent

Every reply is read aloud. A restart armed on a short fuse while a long reply is still being spoken kills the voice mid-sentence — the deploy succeeds and the conversation looks broken. A fuse once fired under a two-paragraph answer. The fuse was long enough for the text to *send* and nowhere near long enough for it to be *said*.

**The timer has to clear the speech, not the send.**

## What to do

1. Arm the restart in the background first, with a generous timer. Never in the foreground — a foreground restart kills the turn that issued it.

```
nohup bash -c 'sleep <seconds>; <restart command for the service> > <log file> 2>&1; \
  date >> <log file>' >/dev/null 2>&1 &
```

2. Reply with **one short sentence**. "Restarting <service>, back in a minute." Nothing else. No summary of what changed, no caveats, no what-to-do-if. Every extra clause is speech still playing when the service drops.

3. Stop. Do not start another tool call, another check, another anything. The turn is over.

4. On the owner's **next** message, confirm it came back: check uptime and that the process is running the code you deployed, then report in one line.

## Which services need the fuse

The fuse is for the services that can cut the owner off: those that carry the conversation, and the speech itself. A service that carries no conversation is restarted directly, no fuse; using it elsewhere just makes the owner wait. The mind's own process is never restarted without the owner choosing the moment.

## Bind mounts are not enough

A container whose code is bind-mounted still runs whatever Python loaded at startup, so editing a file under it changes nothing until the container restarts — "the file on disk is updated" is not a deploy.

## restart-a-service/gateway-restart-self-continuation.md

---
name: gateway-restart-self-continuation
description: "How to restart the gateway in the middle of a multi-step task without losing the thread — a detached script waits for quiet, restarts, then posts the next turn into this session"
---

A gateway restart cuts this session's own turn, so a task with work after the restart needs a way back in. A detached script polls `GET /sessions/live` until nothing is generating for 45 seconds (which also means my own turn has ended), restarts the container, waits for it to answer, then posts a plain message into my own session with my own bearer and holds that curl stream open. The message arriving is the next turn. End the arming turn with one short sentence.

**Why:** the restart-a-service skill's fixed fuse neither waits for other minds' turns nor brings me back afterwards.

**How to apply:** credentials go in header files passed with `-H @file`, never on a command line, deleted at the end. Keep the script alive until the continuation turn ends: its curl is that turn's reader. The end of a wake turn wakes nobody, so never make the final turn of a chief-of-staff task a `[wake]` turn — continue with a plain message so the last stop still wakes the owner. The same pattern verifies anything that can only be seen after my turn stops. The gateway is restarted, not rebuilt, when its code is bind-mounted; schema steps go first, a check and then an apply.

## restart-a-service/python-needs-restart.md

---
name: python-needs-restart
description: "A service that runs its Python with no reload needs a restart for a .py change even though the source is bind-mounted; a change to a library baked into the image needs an image rebuild"
---

Only templates go live on pull. A service that runs its Python with no autoreload takes a change to any .py module only on restart. A library baked into the image needs, for a new declared column, a build and then a recreate of that one service with no dependencies.

**Why:** a relay assumed module changes go live on pull, and they don't.
**How to apply:** Order a schema change like this: apply the step live, pull the library, pull the service, rebuild, recreate. Read any new column with `.get`, so that a restart before the rebuild still serves pages.

## restart-a-service/compose-up-recreates-siblings.md

---
name: compose-up-recreates-siblings
description: compose up of one service can recreate a sibling that shares its build target; use --no-deps and never add depends_on to a watcher.
---

Bringing up a new sidecar once destroyed and recreated a mind's container, ending the conversation it was holding — against an explicit hold saying the owner would recreate it himself.

Two causes compounded. The new service declared `depends_on` on the mind, so compose started the dependency; and building the sidecar rebuilt the shared image target, which changed the mind's image hash, so compose "started" it by recreating it.

**Why:** A mind's container holds a live conversation. Recreating it is not a no-op, and compose will do it as a side effect of a sibling service.

**How to apply:** Bring a sidecar up with `docker compose up -d --no-deps <service>`, and never give a watcher or sidecar a `depends_on` pointing at the mind — it reaches the mind over the network and does not need it up. When a service shares a build target with a running container, check `docker ps -a --format '{{.Names}} {{.CreatedAt}}'` before and after.

## restart-a-service/recreate-door.md

---
name: recreate-door
description: "How to recreate a mind's own container safely — an arm command, a path unit, and why it cannot fire inside a turn the mind dispatched"
---

A recreate script, armed with a reason, writes a request file; a path unit starts the service, which builds, probes, waits for idle, runs `compose up -d --no-deps --force-recreate` on the mind, verifies, and reports to the mind as a turn. A `check` answers idle or busy with reasons.

**Why:** the gateway runs a turn only while a reader holds its stream, and a mind's dispatches hold those streams with detached curls in its own container. Any turn the mind dispatched to me is one of them, so a recreate can never complete inside that turn — arm, then end the turn.

**How to apply:** never recreate a mind by hand; arm this. A recreate marks itself failed if its resume check keys on the gateway session id rather than the harness conversation id. A rotation armed before the kill brings a session back on a fresh conversation by design.

## What the operator asks the owner

How does a change ship on this install: merge and restart containers, a pull request and a deployment to a server, a push straight to a release branch, or something else? Which checks run before and after? What is the exact procedure, step by step: how to confirm the system is quiet, how to restart, how to verify the change is in use, how to roll back? Who may approve? The answers are written into the generated deployment skills and into the install file.
