# Lucent — Knowledge Graph + Vector Memory

## What it is

Lucent is the Hive Mind's self-hosted memory layer. It replaces Neo4j with a SQLite-backed knowledge graph and vector store. All data lives in `/usr/src/app/data/lucent.db` (Docker bind mount — persists across rebuilds).

## Components

| Component | Purpose |
|-----------|---------|
| Knowledge graph | Structured nodes + edges (people, projects, config, identity) |
| Vector store | Semantic memory chunks for fuzzy recall |
| Expiry system | Timed-event nodes auto-expire via nightly sweep |

## Access

Lucent runs as an MCP tool (`hive-mind-tools`) registered in `.mcp.json`. Skills and agents call it via MCP tool calls:

- `graph_query(entity_name, agent_id)` — retrieve a node and its connections
- `graph_upsert(node, agent_id)` — create or update a node
- `memory_store(content, agent_id, data_class)` — write a semantic memory chunk

Each mind has its own `agent_id` namespace (`ada`, `bob`, etc.).

## Data Classes

Defined in `specs/data-classes/`. Each class governs what gets stored and how. Key classes:

- `person` — contacts, relationships
- `technical-config` — system configuration facts
- `ada-identity` / `bob-identity` etc. — mind soul/identity data
- `timed-event` — expiry-aware events
- `preference` — user preferences

## Soul Loading

At session start, `core/sessions.py` calls `_fetch_soul_sync(mind_id)` which does a direct Python import of `lucent_graph.graph_query` and injects the result as a `<soul>` block into the system prompt. No MCP call at spawn time.

## Anti-patterns

- Do not store data in the container without a host bind mount — it won't survive a rebuild
- Neo4j is gone — no Cypher queries, no bolt connection strings
- Do not write directly to `lucent.db` with raw SQL — use the MCP tool layer
