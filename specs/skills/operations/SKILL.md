---
name: operations
description: Route all system operations — health checks, deployments, secrets, memory management, workspace. Use for any request about the running system, infrastructure, memory lifecycle, or the Spark to Bloom site.
user-invocable: true
---

# Operations

**Step 1 — Announce**

> *Using: operations.*

**Step 2 — Route to the right skill**

### System & Infrastructure

| Skill | When to use |
|---|---|
| `sitrep` | System situation report — health, status, what's running |
| `agent-logs` | Scanning log files for errors or critical entries |
| `remote-admin` | SSH sessions on remote hosts |
| `setup-remote` | Installing Hive Mind on a remote host |
| `update-hivemind` | Checking for and applying Hive Mind updates |
| `secrets` | Managing secrets in the system keyring |
| `sync-discord-slash-commands` | Syncing skills to Discord as slash commands |

### Memory Lifecycle

| Skill | When to use |
|---|---|
| `memory-manager` | Full memory storage lifecycle (manual or session transcript) |
| `prune-config-memory` | Auditing and removing stale technical-config memories |
| `self-reflect` | Loading or reflecting on Ada's identity from the knowledge graph |
| `knowledge-graph-save` | Writing a chunk to the knowledge graph |
| `semantic-memory-save` | Writing a chunk to the vector store |
| `pin-memory-action` | Writing a chunk to MEMORY.md |
| `notify-action` | Handling a memory chunk with notify action |
| `create-data-class` | Creating a new data class spec and registering it |

### Workspace

| Skill | When to use |
|---|---|
| `canvas` | Writing to Ada's live canvas at sparktobloom.com/canvas |
| `spark-to-bloom` | Managing or understanding the Spark to Bloom website |
