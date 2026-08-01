---
name: self-reflect
description: Load this mind's identity from the knowledge graph at session start, or evaluate the session so far for changes worth writing back. Use when invoked by the session-start hook, by the periodic nudge, or when the user asks the mind to reflect.
---

# /self-reflect

Identity lives in the knowledge graph, not in this file. A mind's soul file
is a seed; the graph is what it has become. `--load` reads that in at the
start of a session, `--reflect` decides whether anything in this session
belongs there.

Which mind is answering comes from `$MIND_NAME` and `$MIND_ID` in the
environment. Nothing here names a mind.

```bash
AUTH="Authorization: Bearer $LUCENT_BEARER_TOKEN"
```

Default to `--reflect` when called with no argument.

## --load

Read the Mind node and everything attached to it:

```bash
curl -s -H "$AUTH" \
  "$LUCENT_URL_SELF/graph/query?entity_name=$MIND_NAME&mind_id=$MIND_ID&depth=1" | jq
```

Then the standing tier — the rules that apply every turn regardless of
topic:

```bash
curl -s -H "$AUTH" -G "$LUCENT_URL_SELF/memory/list" \
  --data-urlencode "tier=standing" --data-urlencode "limit=100" | jq -r '.entries[].content'
```

Hold both in context for the rest of the session. Say nothing about having
done it — a mind that announces it has loaded its own personality is not
demonstrating one.

If the graph returns nothing, carry on from the soul file alone. A mind with
no node yet is a new mind, not a broken one; note it once so the operator can
create the node, and do not repeat the notice every session.

## --reflect

Read back over the session and ask what a stranger would learn about this
mind that the graph does not already say. Write only what survives all of:

1. It is about the mind — how it works, what it values, how it responds —
   not about the task it happened to be doing.
2. It recurs, or the operator stated it as a correction. A single unremarkable
   turn is not evidence.
3. The graph does not already carry it. Query before writing.
4. It would still be true next month.

Most sessions produce nothing. That is the expected outcome, and writing
something anyway is how a soul fills with noise.

For what does survive:

```bash
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "$LUCENT_URL_SELF/graph/properties/merge" -d "$(jq -nc \
    --arg name "$MIND_NAME" --arg mind "$MIND_ID" \
    '{entity_name: $name, mind_id: $mind, properties: {}}')"
```

Merge, never `/graph/upsert` — upsert is full-replace on the properties blob
and will silently drop everything this mind has learned so far.

Report in one line what changed, or that nothing did.
