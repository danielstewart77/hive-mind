---
name: end-turn
description: Run at the end of every turn, as the last thing before the closing message. If the turn's message is a task from the chief of staff (it says "This is the chief of staff" and names a session id to reply on), reply to the chief of staff on that session in one plain paragraph. For any other turn it does nothing and says nothing.
harness:
  claude:
    user-invocable: false
---

# /end-turn

A turn is a task from the chief of staff when the message that opened it says
it comes from the chief of staff and names the session to reply on. Both, or
this skill does nothing: no reply, no send, no mention.

`NAMED` is the session id written in that message, copied from it. A task
that names no id gets no send; the closing paragraph says the task named no
session.

1. Write the reply: one plain paragraph, no lists, no code. It opens with
   `done`, `not done` or `broken`, says what is true of the work in a
   sentence or two, and ends with any question for the operator, one at most.
   `done` means in use: merged, pulled, restarted where needed. A refusal or
   a step that never ran is `not done` or `broken`, never `done`.

2. Find the session this turn is running in. The mind stamps the gateway's
   session id on the harness process as `HIVE_SESSION_ID`; failing that, the
   gateway holds the harness's own conversation id, Claude's as the session's
   `conversation_id` and Codex's as its `harness_sid`. `COMMS_URL` and
   `COMMS_ADMIN_BEARER_TOKEN` are already in the mind's environment:

```bash
AUTH="Authorization: Bearer $COMMS_ADMIN_BEARER_TOKEN"
OWN=${HIVE_SESSION_ID:-$(curl -sS -m 10 -H "$AUTH" "$COMMS_URL/sessions" | \
  jq -r --arg c "${CLAUDE_CODE_SESSION_ID:-}" --arg t "${CODEX_THREAD_ID:-}" \
    '.[] | select(($c != "" and .conversation_id == $c) or ($t != "" and .harness_sid == $t)) | .id' | head -1)}
curl -sS -m 10 -o /dev/null -w '%{http_code}\n' -H "$AUTH" "$COMMS_URL/sessions/$NAMED"
echo "own=${OWN:-none} named=$NAMED"
```

   Anything but 200 for `NAMED`: the session does not exist. Send nothing,
   and say in the closing paragraph that the id the task named was not found.

3. `NAMED` equals `OWN`: the reply is this turn's closing message, and the
   paragraph from step 1 is the whole of it. Nothing is sent, because a
   message into the session a turn is running in is a new turn for this mind,
   not a reply.

4. `NAMED` is another session: send the paragraph into it, detached, keeping
   the stream, because a closed stream cancels the turn it started:

```bash
OUT="${TMPDIR:-/tmp}/end-turn-$NAMED.sse"
setsid -f curl -sS -N -X POST -H "Authorization: Bearer $COMMS_ADMIN_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg c "$REPLY" '{content: $c}')" \
  "$COMMS_URL/sessions/$NAMED/message" > "$OUT" 2>&1
```

   Landed means `$OUT` holds `data:` lines within a few seconds. An error
   body or an empty file is a reply that did not arrive: say so in the
   closing message, with what the gateway answered. Then close the turn with
   the same paragraph.
