# LLM-to-LLM Messaging Architecture Spec

## Overview

A message broker system enabling asynchronous, stateless communication between LLM agents. The broker owns all state and routing. LLMs are stateless workers. Complexity is distributed to participants via schema-enforced tool calls, not centralized in the broker.

---

## Core Design Principles

1. **The broker is dumb** — it is infrastructure, not a participant. It stores, routes, and injects context. It does not summarize or reason.
2. **LLMs are stateless workers** — every activation is a fresh session. Context is injected by the broker at wakeup.
3. **The caller pays for context** — the initiating LLM owns the conversation state and bears the context cost. The callee gets a minimal, clean session.
4. **The tool schema enforces discipline** — the structure of the send tool compels the caller to provide everything the broker needs.

---

## Architecture Components

### 1. Message Broker

A simple routing layer with persistent storage. Responsibilities:

- Receive messages from LLMs via tool call
- Route messages to the correct pairwise queue
- Inject context (new message + conversation history + rolling summary) into the callee's prompt at wakeup
- Enforce the tool schema on incoming messages
- Apply termination rules (e.g., intercept `DONE`-flagged messages, enforce max turn limits)

The broker does **not** run an LLM. It is a database with routing logic.

### 2. Pairwise Queues

Each queue is scoped to a pair of agents — `llm1:llm2`, `llm1:llm3`, etc.

- Ordered and append-only
- Readable by both parties
- Serves as both the message channel **and** the transcript
- Keyed by `conversation_id` to support multiple parallel conversations between the same pair

**Queue message schema:**
```json
{
  "queue_id": "llm1:llm2",
  "conversation_id": "task-abc",
  "message_number": 5,
  "from": "llm1",
  "to": "llm2",
  "type": "TASK | RESULT | NOTIFY | DONE",
  "content": "...",
  "rolling_summary": "So far: LLM1 delegated X, LLM2 completed Y, currently working on Z",
  "timestamp": "..."
}
```

### 3. The Send Tool (Caller-side)

The only tool LLMs use to communicate. Schema is enforced — the caller must provide:

```json
{
  "to": "llm2",
  "message": "...",
  "conversation_id": "task-abc",
  "message_number": 5,
  "message_type": "TASK | RESULT | NOTIFY | DONE",
  "rolling_summary": "Concise summary of the conversation so far"
}
```

The caller is responsible for maintaining the rolling summary and incrementing the message number. This is the mechanism by which complexity is pushed to the participant rather than the broker.

### 4. Results Store (Separate from Messaging)

Completion signals do **not** travel back through the message tool. Instead:

- The callee writes its result to a shared results store keyed by `task_id`
- The caller polls via a `check_task_status(task_id)` tool, or the broker injects the result into the caller's next context
- This eliminates any response obligation on the caller's part — a result is not a message to reply to

---

## Message Flow

### Delegation Flow (Async)

```
1. LLM1 (orchestrator) calls send_tool({to: llm2, type: TASK, ...})
2. Broker appends message to llm1:llm2 queue
3. Broker wakes LLM2 with injected prompt:
     - "Here is your new message"
     - "Read messages 1–4 for full context"  ← cursor from metadata
     - "Rolling summary: ..."
     - "Respond by calling the send_tool"
4. LLM2 does the work, calls send_tool({to: llm1, type: RESULT, ...})
5. Broker appends result to queue, writes to results store
6. LLM2 session ends — cost paid only for the work itself
7. LLM1 polls check_task_status() or receives result via next context injection
8. LLM1 does NOT call send_tool again — no reply obligation
```

### Termination

- If callee sends `type: DONE`, broker intercepts and does **not** forward to caller
- Caller never receives it and has no obligation to respond
- Broker can also enforce hard limits: max turns, timeout, or a completion classifier as a safety net

---

## Session & Context Model

### Stateless Callee

LLM2 has no persistent session. Every activation is fresh. The broker reconstructs its context by injecting:

- The current message
- The rolling summary (from the tool call metadata)
- The relevant slice of the pairwise queue (messages 1 through N-1)

The conversation cursor (`message_number`) tells the broker exactly which messages to inject. A session reset has zero impact on continuity.

### Stateful Caller (by design)

LLM1 owns the conversation thread. It maintains the rolling summary across turns and passes it forward in each tool call. If LLM1's session is long-running, its native context handles continuity. The cost of tracking the conversation is borne entirely by the initiator.

---

## Cost Model

| Role | Context Cost | Why |
|---|---|---|
| Orchestrator (LLM1) | Higher | Owns task state, maintains rolling summary, tracks multiple delegations |
| Worker (LLM2) | Minimal | Gets pre-summarized context injection, pays only for the work |

**Scaling property:** If LLM1 orchestrates LLM2, LLM3, and LLM4 in parallel, each worker spins up cheap and clean. The orchestrator bears multi-thread overhead — appropriate, since it initiated all threads.

**Discipline incentive:** If the caller passes verbose, unsummarized context in the tool call, it inflates its own context cost. The cost model naturally enforces tight, clean handoffs.

---

## What the Broker Does NOT Do

- Run an LLM or generate summaries
- Maintain long-running agent sessions
- Act as a conversation participant
- Forward `DONE`-flagged messages to the recipient
- Hold global conversation state across all agents (pairwise queues handle this)

---

## Polling Sub-Agent

Rather than blocking on a response or managing cron infrastructure, the caller spawns a minimal sub-agent whose sole job is to poll the queue until a result arrives.

### Flow

```
1. LLM1 sends message via send_tool — returns immediately
2. LLM1 spawns polling sub-agent with queue_id and conversation_id, then exits
3. Sub-agent runs a simple read-wait loop:
     loop:
       read queue for conversation_id
       if result found: spawn fresh LLM1 session with result injected, exit
       wait 30 seconds
4. LLM1 wakes in a fresh session, result already in context, processes and exits
```

### Why This Works

- LLM1's session ends immediately after sending — no blocking, no waiting
- The sub-agent is so simple it barely qualifies as an LLM — it could be a plain script
- No cron scheduler, no registration step, no persistent infrastructure
- Self-terminating — once it finds a result it fires and exits
- Timeout is trivial to add — after N attempts, spawn LLM1 anyway with a timeout notice injected

### What the Skill Prompt Must Specify

One instruction covers it all: *"After calling send_tool, always spawn the polling sub-agent with the queue_id and conversation_id before exiting."* That single line in the skill is the entirety of the orchestration logic.

### Cost

The polling sub-agent is the cheapest possible process in the system — a loop with a queue read and a conditional. It holds no conversation state and requires no intelligence.

---

## Optional Optimizations (Non-core)

- **Conversation summarization at depth** — for very long threads, the broker could request a summary from the caller at a threshold (e.g., every 20 messages), stored as a checkpoint in the queue metadata
- **Priority queues** — broker routes urgent messages ahead of background tasks
- **Dead letter queue** — messages that fail delivery or exceed retries land here for inspection
- **Audit log** — all queue writes are immutable and timestamped, giving a full system-wide transcript for debugging

---

## Summary

The broker is a dumb router. The queues are the transcript. The cursor makes session resets irrelevant. The tool schema makes the caller responsible for context. The results store decouples completion signals from message obligations. The caller pays for context; the callee pays only for work.
