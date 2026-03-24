# Data Class Index

This file lists every available memory data class and its spec file.
It is the first thing `classify-memory` reads before matching any chunk.

When a new data class is created via `create-data-class`, that skill will
add an entry here automatically. The first entry was added manually.

---

## Available Data Classes

| Class Name | Spec File | Storage Bucket | Description |
|------------|-----------|----------------|-------------|
| `technical-config` | `technical-config.md` | vector | Stable technical facts about how the Hive Mind system is built, configured, or operates |
| `project-task` | `project-task.md` | — | Planka cards, backlog items, story events — always discarded |
| `person` | `person.md` | both | A named individual with facts about their relationship to Daniel, role, or notable details |
| `ada-identity` | `ada-identity.md` | both | Confirmed facts about who Ada is — origin, character, self-knowledge, identity corrections confirmed by Daniel |
| `future-project` | `future-project.md` | both | Planned or future projects Daniel intends to build — architecture, hardware, constraints, goals — not yet implemented |
| `ephemeral` | `ephemeral.md` | — | Point-in-time data with no lasting relevance — weather, live prices, status snapshots — always discarded |
| `news-digest` | `news-digest.md` | — | Raw news content from newsletters or feed runs (TLDR, X AI Lurker, InfoSec roundups) — always discarded unless Daniel engaged with it, in which case reclassify the engaged chunk |
| `preference` | `preference.md` | vector | A stable preference, habit, or behavioral tendency belonging to Daniel or Ada |
| `news-event` | `news-event.md` | — | External news or world incidents — always discarded |
| `intention` | `intention.md` | vector | A stated near-term plan from Daniel not yet acted on — lighter than future-project |
| `timed-event` | `timed-event.md` | vector | A future scheduled event with a specific datetime; requires expires_at |
| `nagatha-identity` | `nagatha-identity.md` | both | Confirmed facts about who Nagatha is — origin, character, values, domain strengths, communication style |

---

## How to Add a New Entry

Add a row to the table above with:
- **Class Name** — the short identifier (e.g., `person`, `preference`)
- **Spec File** — relative path from this directory (e.g., `person.md`)
- **Storage Bucket** — `vector`, `graph`, or `both`
- **Description** — one sentence on what this class covers
