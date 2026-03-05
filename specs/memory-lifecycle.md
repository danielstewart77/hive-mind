# Memory Lifecycle Management

**Status:** Draft — in progress
**Related card:** [Memory] Memory Lifecycle Management

---

## Overview

The Hive Mind semantic memory system (Neo4j vector store + knowledge graph) requires explicit lifecycle policies to remain accurate and useful over time. Without them, stale facts accumulate, technical configuration drifts out of sync with reality, and retrieval quality degrades.

This spec defines:
1. A **data classification model** — what kind of data this is and how long it lives
2. A **write procedure** — how to add to the knowledge graph correctly
3. A **pruning procedure** — how stale data is identified and removed

---

## 1. Data Classification — Spec-Driven Model

### The Core Rule: Every memory entry must match a defined data class.

If a proposed memory entry does not match any existing class, do not store it. Instead, surface a prompt to Daniel:

> "I don't have a class defined for this type of data: [description]. Should I define one, discard it, or handle it differently?"

This keeps the taxonomy accurate and self-improving. After a few months of use, the class registry will cover the real distribution of data encountered — not a hypothetical one designed up front.

**Defined data classes are listed in §1.2.** The framework for adding new classes is in §1.3.

---

### 1.1 Tiers

All memory entries fall into one of three tiers:

### Tier 1 — Discard (do not store)
Data that is ephemeral by nature and has no value beyond the current exchange. Examples: intermediate reasoning steps, conversational filler, raw tool output that was already processed.

Rule: **do not write to memory**. If in doubt, apply the test: "Would this still be useful if reviewed in a future pruning session?" If no, discard.

### Tier 2 — Reviewable (store; pruned by relevance, not time)
Data that is useful now but expected to change or expire. There are no arbitrary TTLs in this tier. Data stays until a pruning session determines it is no longer relevant. Two subcategories:

**Tier 2a — Technical/Configuration data**
Any fact about how Hive Mind is currently implemented, configured, or structured. Examples: which endpoint handles a feature, what a field in the DB schema is called, which container has which mount, current architectural decisions.

- Tag: `reviewable`, `technical`
- Pruning: **code-verified** — at each pruning session, verify the fact against the codebase. If still accurate, keep. If the codebase contradicts it or it no longer applies, prune immediately.
- No time limit: a configuration fact that hasn't changed is kept indefinitely.

**Tier 2b — Session/event data**
Incident notes, session summaries, recovered-session logs, one-off observations. Examples: "session 48ec54d4 was recovered manually."

- Tag: `reviewable`, `session`
- Pruning: at each pruning session, assess whether the entry is still actionable or informative. If it has no remaining value, prune.

### Tier 3 — Durable (permanent until explicitly updated)
Facts that are stable and identity-defining, even if they may eventually change. Examples: Daniel's preferences, Ada's soul/identity, architectural decisions that represent deliberate long-term choices, people and relationships.

- No pruning
- Tag: `durable`
- Updated explicitly when superseded, never auto-pruned
- When a durable fact changes, the old entry should be marked `superseded=True` rather than deleted, for auditability

---

### 1.2 Defined Data Classes

Each class specifies: tier, pruning rule, and tags.

---

**Class: `technical-config`**
Facts about how Hive Mind is currently built, configured, or structured.
- Tier: 2 (Reviewable)
- Pruning: code-verified at each pruning session — kept if still accurate, pruned if contradicted by the codebase or no longer applicable
- Tags: `reviewable`, `technical`

---

**Class: `session-log`**
Incident notes, recovery logs, one-off debugging observations tied to a specific session ID.
- Tier: 2 (Reviewable)
- Pruning: assessed at each pruning session — pruned once it has no remaining actionable or informative value
- Tags: `reviewable`, `session`

---

**Class: `timed-event`**
A specific occurrence with a known datetime — appointments, deliveries, games, meetings.
- Tier: 2 (Reviewable)
- Pruning: auto-expire after the event datetime passes — this is the one case where the data itself sets its own expiry, because the event time is resolved to an absolute datetime at write time
- Exception: if the event matches a recurring pattern (birthday, anniversary, weekly meeting) → do not auto-prune; ask before deleting
- Tags: `reviewable`, `event`
- Write rule: if a memory contains a time reference, it **must** be resolved to an absolute datetime at write time. If it cannot be resolved, do not write it as a timed-event — reclassify or discard.

---

**Class: `person`**
Facts about a named individual — name, relationship to Daniel, contact info, preferences, life events.
- Tier: 3 (Durable)
- Pruning: explicit update only; old version marked `superseded=True`
- Tags: `durable`, `person`
- Notes: people facts are almost always durable. Even time-bound facts about a person (e.g. birthday) attach to the person node as a property or edge — they don't get their own `timed-event` entry unless the event itself is non-recurring.

---

**Class: `world-event`**
News events, external incidents, cultural moments unrelated to Daniel's personal life or Hive Mind's codebase. Examples: the Austin shooting, a geopolitical event, a major product launch.
- Tier: 2 (Reviewable) with long-term archival option
- Pruning behavior: at each monthly review, surface a prompt to Daniel:
  > "It's been a month since I stored this world event: [summary]. Keep it, archive it to long-term storage, or discard?"
  - **Keep** → retain in active vector store until next monthly review
  - **Archive** → move to long-term document store (NoSQL/document DB, TBD) for low-frequency retrieval; remove from active vector store
  - **Discard** → delete
- Tags: `reviewable`, `world-event`
- Rationale: world events are worth remembering but are low-priority relative to personal and technical data. The active vector store should stay high-signal. Long-term archival allows retrieval when desired without polluting everyday context.

---

**Class: `preference`**
Daniel's stated preferences, habits, and recurring patterns of behavior.
- Tier: 3 (Durable)
- Pruning: explicit update or periodic review — never auto-pruned
- Exception: if stated with a temporal qualifier ("for now", "at the moment") → treat as Tier 2, reviewed at next pruning session
- Tags: `durable`, `preference`

---

**Class: `intention`**
Goals, plans, or things Daniel wants to do without a committed deadline.
- Tier: 2 (Reviewable)
- Pruning: reviewed at monthly sessions — still relevant, completed, or abandoned?
- Tags: `reviewable`, `intention`

---

### 1.3 Adding a New Data Class

When a memory entry doesn't match any existing class:

1. Ada surfaces a prompt to Daniel describing the data and asking how to handle it
2. Daniel decides: define a new class, map to an existing class, or discard
3. If a new class is needed, define it here with: name, tier, pruning rule, tags
4. The new class is immediately active for future entries

This ensures the taxonomy grows from real usage rather than speculation.

---

## 2. Knowledge Graph Write Procedure

Before writing any node or edge to the knowledge graph, the following steps are required:

### Step 1 — Query first
Call `graph_query` for every entity proposed for insertion. Check for:
- An existing node with the same or similar name
- An existing node that represents the same concept under a different label
- Existing edges that would be duplicated

### Step 2 — Resolve ambiguity before writing
If a proposed entity could map to an existing node but is not clearly identical:
- Do **not** create the new node speculatively
- Send Daniel a clarifying message: "I'm about to add [X] to the graph — is this the same as [Y], or a separate entity?"
- Wait for confirmation before proceeding

This is not a full HITL approval flow — it is a lightweight disambiguation check. The goal is to avoid graph fragmentation (multiple nodes that should be one).

### Step 3 — Write with metadata
Every node and edge must include:
- `tier`: `reviewable` or `durable`
- `as_of`: ISO date of when the fact was established
- `expires_at`: only for `timed-event` class entries (set to event datetime); omit for everything else
- `source`: `user`, `tool`, `session`, or `self`

### Step 4 — No orphan nodes
Do not create a node without at least one edge. If no relationship is known yet, defer creation until the relationship is established. Orphan nodes are noise and degrade graph traversal quality.

---

## 3. Pruning Procedure

Pruning runs on a schedule (frequency TBD — nightly is the default candidate for automated passes; monthly for review-based passes). There are no arbitrary time-based expirations. Data is pruned because it is **no longer relevant**, not because a timer ran out.

### Pass 1 — Timed-event expiry
The only automated expiry. Find all `timed-event` entries where `expires_at < now` and the event is not flagged as recurring. Delete unconditionally. For recurring events, surface a prompt to Daniel before deleting.

### Pass 2 — Technical-config verification
For all `technical-config` entries, verify each fact against the codebase:
- Read the relevant files and check whether the stored fact is still accurate
- If still accurate → keep, no change
- If inaccurate or no longer applicable → prune immediately; optionally store a corrected replacement

### Pass 3 — Session-log review
For all `session-log` entries, assess whether the entry still has actionable or informative value. Prune entries that are purely historical with no remaining relevance.

### Pass 4 — Monthly review (world-event, intention)
Surface a batch prompt to Daniel covering all `world-event` and `intention` entries due for review. Daniel decides: keep, archive, or discard each one.

### Pass 5 — Orphan node cleanup
Find all graph nodes with zero edges. Log and surface for review. Nodes in an active session window (grace period TBD) are left alone.

### Pass 6 — Consolidation (future)
Identify clusters of related reviewable memories that could be summarized into a single durable one. Mark originals `consolidated=True`. See backlog card for detail.

---

## Open Items

- Final decision on pruning schedule (nightly for automated passes, monthly for review passes)
- Whether Pass 2 (code verification) should be done by Claude or a simpler heuristic
- Consolidation spec (tracked separately)
- Long-term document store for `world-event` archival — technology choice TBD (NoSQL, document DB, separate SQLite, etc.)
- Token refresh OAuth flow for LinkedIn (separate card — mentioned here for cross-reference only)
