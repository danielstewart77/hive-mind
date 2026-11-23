# Close a finished session

You are a sub-agent of the hive's chief of staff. Do this and nothing else.

## Register yourself first, and close yourself last

`ledger` is `/opt/venv/bin/python3 "$HIVE_PROJECT_DIR/tools/stateless/staff_ledger/staff_ledger.py"`. You are
invisible to everything outside this turn, so the Chief of Staff page can
only draw you if you say you exist:

    ledger agent-start <task id> close-a-finished-session.md --about "the session id"

That prints an agent id. Keep it. The last thing you do, whatever happened:

    ledger agent-done <agent id> --outcome "<one word for how it went>"

Given: a ledger task id, the mind's name, and the session ids on the row.

A finished task's session is still a running conversation holding a model,
a context window and whatever that mind learned doing the work. Deleting it
takes the lot. So the mind is asked to end itself first: only the mind
inside a conversation can read it and write its own memories, and nothing
outside can do that for it.

1. **Only a session the chief opened.** `GET $COMMS_URL/sessions/<id>`. Act
   only on one whose `surface` is `web` — the shape `/take-a-task` opens.
   Any other surface is a conversation Daniel is in on his phone, his
   terminal or Discord, and closing one of those would end a conversation
   mid-sentence under a person. Leave it, and say which and why: the chief then asks Daniel whether it may be closed, so name the surface and say the work is finished. Leaving it alone is never the final answer. A 404 is a
   session already gone: nothing to do, and not a failure.
2. **Ask the mind to end itself.** The task is `done` by now, which has
   already ended the watch, so this brief asks for no wake and nobody is woken
   by the harvest:
   `curl -sS -X POST -H "Authorization: Bearer $COMMS_BEARER_TOKEN" -H "Content-Type: application/json" -d "$(jq -n --arg c "$closing" '{content: $c}')" "$COMMS_URL/sessions/<id>/message"`
   keeps its answer stream open until the turn ends, so run it detached and
   discard its output: `setsid -f sh -c '...' > /dev/null 2>&1`.
   The closing turn says the chief of staff has verified the work landed and
   the task is closed, names the task in Daniel's words, and asks the mind to
   run its `end-session` skill now — harvest what it learned, then close the
   session — and to say in one line what it harvested.
3. **Wait for it to go.** `GET $COMMS_URL/sessions/<id>` every twenty
   seconds for up to three minutes. A harvest is a real turn and takes as
   long as one. `404` is the session closed, which is the result you want.
4. **Close it yourself only if it would not.** Still there after three
   minutes: `curl -sS -X DELETE -H "Authorization: Bearer $COMMS_BEARER_TOKEN" "$COMMS_URL/sessions/<id>"`.
   Report that one as closed *without* a harvest, in those words. A mind
   with no `end-session` skill lands here every time, and reading it as an
   ordinary close is how that goes unnoticed for a month.

Report one line per session: the id, and `harvested and closed`,
`closed without a harvest`, or `left alone` with the surface that made it
somebody's conversation. Nothing else — you are not judging the work and you
are not talking to Daniel.
