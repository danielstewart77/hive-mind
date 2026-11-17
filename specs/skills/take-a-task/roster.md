# The roster

This is the shipped shape, with no minds in it. A roster describes one
hive's minds, so its entries are written on that hive and are never sent
back to the repository: draft one per mind registered in comms
(`GET /broker/minds`) from what each mind's soul and `runtime.yaml` say,
and have Daniel correct it. **Role** is Daniel's to state and correct; the
chief never edits it. **Chief's notes** are the chief's own, added as it
learns.

## Routable

### <mind name>
- **Role:** What Daniel says this mind is for, dated when he said it.
- **Chief's notes:** What working with this mind has taught you: its
  harness and model, what it is good and bad at, how its reports need
  reading, and anything Daniel has said about using it.

## Not routed to

- **<your own name>** — the chief of staff itself.
- A mind that is not Daniel's to task, and any registry row that is not a
  mind, each with the reason.

## Inventory (broker registration versus the mind's own report)
- <mind name>: harness and model as registered; what the mind itself says
  it runs; where it runs. Note a mismatch rather than resolving it.

## Standing preferences from Daniel
- 2026-11-12: Do not ask to close finished work; tell him it is done so he can catch a miscommunication, and close it.
- 2026-11-13: When a mind lacks the access or permission a task needs, do not bring Daniel the question; hand that part to the operator mind.
- 2026-11-15: Restarts and recreates of other minds and services are the operator mind's to run, not Daniel's. Never hold a restart for a feature; make it safe (confirm nothing is in flight first) and run it at the first safe moment, because Daniel wants things shipped and in use. The chief arms its own recreate when idle.
- 2026-11-17: Confirmed functionality is merged, rebuilt, restarted or recreated, whatever the container or target needs to be complete. The mind waits until the system is quiet and picks an idle moment; only an idle moment that cannot be found comes back to Daniel. Never tell a mind not to restart as a limit of the chief's own. The gate on a merge is the mind being quite sure it is clean and disrupts nothing in flight, for itself or any other mind.
- 2026-11-17: Worktrees are always cleaned up. Once work is merged back into main or master, its worktree is deleted, locally and on the remote. Every brief that assigns code work carries it as part of what done means.
- 2026-11-17: A test may never insert anything into production. Code whose job is to write into production is not wrapped by a test until Daniel has been asked how it can be tested safely; such tests that exist are deleted.
- 2026-11-17: Keep work consolidated. Fit a new request under something already on the board or the ledger, as a story under a feature or a task under a story, and ask when it is close but not certain, rather than opening another similar task. Written into take-a-task step 3a.
