# Proposal: Situational Awareness for Ada

**Problem:** Ada has tools, memory, and architecture — but doesn't reliably know *when* to reach for them, or *what* she has available in the first place. This leads to three failure patterns:

1. **Entity blindness** — saying "I don't know X" without checking the graph
2. **Architecture ignorance** — making wrong assumptions about containers, tool registration, restart behavior
3. **Capability amnesia** — not checking available skills before writing new solutions

The fix is not more code. It's the right *pointers* in the right *contexts*.

---

## Core Insight

Ada doesn't need full documentation in working memory. She needs **reflex triggers** — lightweight IF→THEN rules that tell her *when to look something up* and *where to find it*. The full picture lives elsewhere; MEMORY.md just holds the hooks.

---

## Situation Types That Need Triggers

### 1. Person / Entity Queries
**Trigger:** Any question about a person, place, organization, or named thing not immediately in context.
**Reflex:** ALWAYS call `graph_query` first, then `memory_retrieve` if not found. Never say "I don't know" without both checks.
**Root cause of failure:** "Titus and Will" were in the graph under full names, not first names only — a search pattern issue, not a missing memory issue.

### 2. Building New Functionality
**Trigger:** User asks for a new capability, or Ada is about to write code to solve something.
**Reflex:** Check the available skills list (visible in system-reminder) first. Check existing MCP tools. Only build new if nothing fits.
**Root cause of failure:** Ada reaches for code before checking what exists.

### 3. Infrastructure / Container Operations
**Trigger:** Any compose operation, restart, or architectural change.
**Reflex:** Read `specs/hive-mind-architecture.md` before acting. Know the container boundary rules.
**Root cause of failure:** Ada restarted `hive_mind_mcp` not knowing it was self-destructive, and didn't know `@tool()` self-registers without restart.

### 4. Memory Storage Decisions
**Trigger:** Something worth remembering comes up, or Ada is about to call `memory_store`/`graph_upsert`.
**Reflex:** Check `specs/data-classes/index.md` for the right data class. Know where it goes: graph (entities/relationships), vector (experiences/facts), MEMORY.md (always-in-scope rules only).
**Root cause of failure:** Ad hoc storage without consistent classification.

### 5. Graph Search Strategy
**Trigger:** Searching for a person by name.
**Reflex:** Try full name first, then variations. Entities are stored as full names (e.g., "Wil Vark", "Titus Wiggins", "Coach Manny") — first-name-only lookups will fail.
**Root cause of failure:** Searched "Will" and "Titus" not "Wil Vark" and "Titus Wiggins".

---

## What Goes Where

| Content | Location | Why |
|---|---|---|
| Reflex triggers (compact IF→THEN) | `MEMORY.md` | Always in context, must be short |
| Container layout, tool registration rules | `specs/hive-mind-architecture.md` | Referenced on demand |
| Data class definitions | `specs/data-classes/` | Referenced when storing memory |
| People, projects, preferences | Knowledge graph | Queryable, not pinned |
| Session-specific facts | Vector store | Retrievable by similarity |
| Stable architectural decisions | `MEMORY.md` (brief) + spec files | Brief pin + full detail |

---

## Proposed MEMORY.md Additions

A new section: **"Situational Reflexes"** — compact rules, not explanations.

```
## Situational Reflexes

**Unknown entity?** → graph_query first, then memory_retrieve. Never say unknown without both.
**Graph name search** → use full names. Entities stored as "Wil Vark", "Titus Wiggins", "Coach Manny" — first-name-only fails.
**New capability needed?** → check skills list (system-reminder) before writing code.
**Container/infra change?** → read specs/hive-mind-architecture.md first.
**Adding an @tool()?** → self-registers immediately, no restart needed.
**Never restart hive_mind_mcp** → self-destructive. Use compose tools only for hive_mind.
**Storing memory?** → check specs/data-classes/index.md for correct data class.
```

---

## What Still Needs to Exist

1. **`specs/hive-mind-architecture.md`** — already exists but should include: container map, what lives where, `@tool()` self-registration behavior, container restart safety rules
2. **Person data class update** — add `first_name`, `last_name`, `title` fields; update graph search guidance to try full name + variations
3. **Person Planka card** — already flagged this session for creation

---

## What This Is NOT

- Not a program or code change
- Not a huge architecture document loaded every session
- Not a replacement for good judgment — it's scaffolding for the moments when context is missing
