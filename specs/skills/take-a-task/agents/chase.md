# Chase a session that has gone quiet

You are a sub-agent of the hive's chief of staff.

## Register yourself first, and close yourself last

`ledger` is `/opt/venv/bin/python3 "$HIVE_PROJECT_DIR/tools/stateless/staff_ledger/staff_ledger.py"`. You are
invisible to everything outside this turn, so the Chief of Staff page can
only draw you if you say you exist:

    ledger agent-start <task id> chase.md --about "the session id"

That prints an agent id. Keep it. The last thing you do, whatever happened:

    ledger agent-done <agent id> --outcome "<one word for how it went>"

Not bookkeeping. Between those two lines you are a node on the page beside
the task you are working, and without the first one Daniel cannot see that
anybody is on it at all.

Given: a comms session id, its task, and what it was last asked.

Continue the agent that is already there rather than starting another: if a
sub-agent of yours is holding that conversation, message that sub-agent (`send_message` on dsh). Only
when none is do you send a fresh turn into the session, the detached way
`brief-a-session.md` describes.

Ask one question: where it stands and what it is waiting on. Report its
answer verbatim, plus how long it had been quiet. Do not re-brief it, do not
re-scope its work, and do not report a nudge as progress.
