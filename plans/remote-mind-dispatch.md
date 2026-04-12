# Remote Mind Dispatch — Cross-Instance Communication

> **Status:** Not yet implemented. Implement after container isolation migration is complete.

---

## Problem

The broker's `wakeup_and_collect` function only handles local minds — it calls the local session manager to create sessions and send messages. When a mind is `remote: true` (running on a separate host with its own Hive Mind installation), the broker cannot reach it.

## Goal

Enable bidirectional messaging between minds on different Hive Mind installations. A message from Ada (Machine A) to Sergeant (Machine B) should work the same way as a message from Ada to Nagatha (local).

## Architecture — Federated Model

Each Hive Mind installation is a full peer with its own nervous system, broker, and minds. Cross-instance communication routes through HTTP between gateways.

```
Machine A (workstation)                    Machine B (NetSage)
┌──────────────────────┐                   ┌──────────────────────┐
│ Nervous System       │                   │ Nervous System       │
│ ├── Gateway (:8420)  │◄── HTTPS+APIkey──►│ ├── Gateway (:8420)  │
│ ├── Broker           │                   │ ├── Broker           │
│ └── Session Manager  │                   │ └── Session Manager  │
│                      │                   │                      │
│ Minds:               │                   │ Minds:               │
│ ├── ada (local)      │                   │ ├── sergeant (local) │
│ ├── bob (local)      │                   │ └── ada (remote)     │
│ ├── nagatha (local)  │                   │                      │
│ └── sergeant (remote)│                   │                      │
└──────────────────────┘                   └──────────────────────┘
```

Each side registers the other's minds as `remote: true` with the external `gateway_url`.

## What Needs to Change

### 1. Broker remote dispatch

`wakeup_and_collect` in `core/broker.py` currently does:
```python
session = await session_mgr.create_session(owner_type="broker", mind_id=to_mind)
await session_mgr.send_message(session_id, wakeup_prompt)
```

It needs to branch on whether the target mind is remote:

**Local mind:** current behavior — use the local session manager.

**Remote mind:**
- Look up the mind's `gateway_url` from the registry
- Retrieve the remote gateway's API key from the keyring
- `POST {gateway_url}/sessions` with API key auth → create session on the remote gateway
- `POST {gateway_url}/sessions/{id}/message` → send wakeup prompt
- Collect the response from the remote SSE stream
- Write the response as a completed message in the local broker

The remote dispatch uses `aiohttp` (already a dependency) to make the HTTP calls.

### 2. Remote API key storage

When registering a remote mind via `/add-mind`, the skill needs to:
- Ask for the remote gateway's API key
- Store it in the keyring scoped to that mind (e.g. key: `<name>_gateway_api_key`)
- The broker retrieves it at dispatch time

### 3. Bidirectional registration prompt

When `/add-mind` registers a remote mind (e.g. Sergeant on Machine A), the skill should remind the user:
- "For Sergeant to message minds on this system, you also need to register your minds as remote on Sergeant's installation."
- Optionally: offer to generate the `MIND.md` files for the remote side so the user can copy them.

### 4. Gateway authentication for incoming remote requests

The gateway already has API key auth for external requests (Phase 2D). When Machine B's broker dispatches a message to Machine A, it authenticates with Machine A's API key. This is already handled by the gateway security layer.

### 5. Error handling

The existing error contract applies: if the remote gateway is unreachable, return `503` with `mind_unreachable`. The calling mind decides whether to retry. No change needed — just verify it works across the network.

## What Does NOT Need to Change

- **Message format** — same `POST /broker/messages` payload
- **Polling** — the polling agent on the sending side works the same way (polls local broker)
- **Broker tables** — `conversations` and `messages` tables are local to each installation. No cross-instance DB sharing.
- **`/send-message-to-mind` skill** — the skill posts to the local broker. The broker handles local vs remote dispatch transparently.

## Open Questions

- **Message delivery guarantees.** If Machine B is temporarily down, should Machine A's broker retry the dispatch? Or just mark it failed and let the caller handle it? Current answer: fast-fail (consistent with the existing error contract). But for cross-network, transient failures are more common — worth reconsidering.
- **Latency.** Cross-network dispatch adds HTTP round-trip latency to every message. For most use cases this is fine (async messaging is already tolerant of delay). But the collection backstop timers may need adjustment for remote minds.
- **Discovery.** Currently, remote minds are registered manually via `/add-mind`. Should there be a discovery protocol where two Hive Mind installations can exchange mind rosters automatically? Future scope.
