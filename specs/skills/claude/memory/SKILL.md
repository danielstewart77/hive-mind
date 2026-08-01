---
name: memory
description: Access the mind's memory store — vector recall and knowledge graph. Use when querying entities, relationships, or recalling and storing memories.
argument-hint: [operation] [...]
tools: Bash
user-invocable: true
---

# Lucent — Knowledge Graph & Vector Memory

## Overview

Lucent is the shared `hive-lucent` container (built from the upstream
hive-mind repo's `nervous-system/` directory), reached over HTTP with a
bearer token. Two surfaces:

- **Graph** — entities (Person, Project, System, Concept, Preference) and their relationships
- **Memory** — semantic vector store of free-form text, retrievable by embedding similarity

## When to use

- Recall a person, project, or fact
- Search prior memories by topic
- Store something durable that should persist across sessions
- Augment a response with background knowledge

## Setup (prepend before every invocation)

```bash
AUTH="Authorization: Bearer $LUCENT_BEARER_TOKEN"
```

## Graph operations

**Query by name** (1-3 hops):

```bash
curl -s -H "$AUTH" "$LUCENT_URL_SELF/graph/query?entity_name=Alex&mind_id=$MIND_ID&depth=1" | jq
```

**Upsert a node**:

```bash
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "$LUCENT_URL_SELF/graph/upsert" -d '{
    "entity_type": "Person",
    "name": "Alex",
    "properties": {"role": "chef-aspirant"},
    "relation": "SON_OF",
    "target_name": "<user-name>",
    "target_type": "Person",
    "mind_id": "'"$MIND_ID"'",
    "data_class": "current-state",
    "source": "self"
  }' | jq
```

**Additive property update** — `/graph/upsert` is full-replace on the
`properties` blob; use `/graph/properties/merge` to add or update keys
without clobbering the rest.

## Memory operations

**List recent memories** (paginated):

```bash
curl -s -H "$AUTH" "$LUCENT_URL_SELF/memory/list?offset=0&limit=10&mind_id=$MIND_ID" | jq
```

**Semantic search** (top-k by embedding similarity):

```bash
curl -s -H "$AUTH" "$LUCENT_URL_SELF/memory/retrieve?query=cooking+ingredients&k=5&mind_id=$MIND_ID" | jq
```

**Store a new memory**:

```bash
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "$LUCENT_URL_SELF/memory/store" -d '{
    "content": "Example durable fact",
    "data_class": "current-state",
    "mind_id": "'"$MIND_ID"'",
    "tags": "config",
    "source": "self"
  }' | jq
```

## Notes

- Write operations include server-side guards (orphan detection, disambiguation, identity checks). They may return `{"upserted": false, "reason": "..."}` — read the reason and decide.
- `mind_id` is always the mind's UUID (`MIND_ID` from `.env`), never the display name. The graph and memory stores are scoped per mind; lucent's identity guard rejects Mind-node writes whose `mind_id` doesn't match.
- `tier=standing` writes are guarded server-side: they require `source=always-remember`.
