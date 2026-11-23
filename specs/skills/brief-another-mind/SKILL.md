---
name: brief-another-mind
description: Brief another mind of your owner and be woken when it stops, without holding a connection open. Trigger when you send work to another mind's session and want to know when its turn ends, when you are woken about a session you briefed, when you want its latest answer, or when you are finished with a task you were watching.
---

# /brief-another-mind

The gateway does the waiting. You send a brief and ask to be woken; the
gateway runs the turn to its end with nobody reading, and drops one turn into
*this conversation* every time the briefed session stops. You never hold a
stream open, save one to a file, or poll.

Every call goes to `$COMMS_URL` with your own credential:
`-H "Authorization: Bearer $COMMS_BEARER_TOKEN" -H "Content-Type: application/json"`.
The gateway accepts it for any mind whose owner is yours. Not the broker, and
not the admin token. `$HIVE_SESSION_ID` is this conversation.

## 1. Ask to be woken, with the brief

Open or pick the session on the other mind (`POST /sessions` with its
`mind_id`), then:

```
curl -sS -X POST -H "Authorization: Bearer $COMMS_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg c "$brief" --arg s "$HIVE_SESSION_ID" --arg t "$task" \
        '{content: $c, wake: {sender_session_id: $s, task: $t}}')" \
  "$COMMS_URL/sessions/<session id>/message"
```

`$t` is the task in a few words; the wake quotes it back. The answer is `202`
with `accepted: true` and a `watch`. The first time you ask about a session
the `watch` carries a `capability`: **keep it**, it is the only thing that can
mark the task finished and it is shown once. A brief sent without `wake`
creates no watch and wakes nobody.

Send the brief from a conversation that will still exist when the answer
comes. A wake goes only to the conversation that sent the brief, and is
dropped if that conversation was ended, including by `/new` or `/clear`.

If you briefed a session before using this, ask without a brief:
`POST /sessions/<id>/watch` with `{"sender_session_id": "$HIVE_SESSION_ID",
"task": "...", "already_briefed": true}`.

## 2. What a wake looks like

A turn in your conversation beginning `[wake]`, naming each session that
stopped and its task, and saying whether it ended its turn or died. If you
were mid-turn when sessions stopped you were not interrupted: they arrive
together, as one message, once you are idle. It does not carry the answer.

The stop of a session you did not brief for yourself wakes you too: every stop
of a watched session does, including a turn Daniel started in it himself.

## 3. Fetch the answer

`GET $COMMS_URL/sessions/<session id>/answer`: `state` (`working` or `idle`),
`outcome` (`ended` or `died`), `answer` and `stopped_at`. As often as you like;
it returns the same until a newer turn ends, mid-turn it says `working` and
returns no answer, and it still works after a gateway restart or after your
own rotation.

## 4. Decide

A wake is not a message to reply to. Read the answer and do one of two things:

- brief the session again, because there is a real new instruction to give; or
- mark the task finished.

Never send an acknowledgment or a thank-you. It would be a fresh turn for the
other mind, and its stop would wake you again.

## 5. Mark it finished

Saying so in prose ends nothing, and neither does the other mind reporting
"done": that is only a stop. The one act is:

```
curl -sS -X POST -H "Authorization: Bearer $COMMS_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg s "$HIVE_SESSION_ID" --arg c "$capability" \
        '{sender_session_id: $s, capability: $c}')" \
  "$COMMS_URL/sessions/<session id>/watch/finish"
```

Later stops of that session wake nobody, and wakes still waiting for it are
discarded. Doing it twice is harmless. `GET $COMMS_URL/sessions/$HIVE_SESSION_ID/watches`
lists what you are still going to be woken about.

## Do not chain

If you were briefed with a wake requested, do not brief a third mind as part
of that task: its stop would wake you, never the mind that briefed you, so
the one who asked would not hear the result. The gateway enforces nothing
here; this is the rule.
