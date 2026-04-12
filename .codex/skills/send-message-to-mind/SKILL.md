---
name: send-message-to-mind
description: Send an async message to another mind via the broker. Handles conversation ID generation, rolling summary, metadata population, and polling agent spawn.
---

# send-message-to-mind

Send an asynchronous message to another mind via the message broker.

## When to use

Use this skill when delegating a task to another mind, or sending a follow-up turn in an ongoing inter-mind conversation.

## Step 1 — New conversation or follow-up?

- **New conversation:** generate a UUID as `conversation_id` and a UUID as `message_id`. Set `rolling_summary` to `""`.
- **Follow-up (turn 2+):** reuse the existing `conversation_id`. Generate a new `message_id`. You MUST write a rolling summary before sending (Step 3).

## Step 2 — Select request_type

Choose the type that best describes the task. This determines notification thresholds if the task runs long.

| `request_type`         | Description                                   | Threshold |
| ---------------------- | --------------------------------------------- | --------- |
| `quick_query`          | Simple question, fast expected response       | 5 min     |
| `research`             | Web lookup, CVE search, information gathering | 20 min    |
| `code_review`          | Reviewing a PR or implementation              | 20 min    |
| `content_generation`   | Writing, summarising, formatting              | 15 min    |
| `data_analysis`        | Query processing, log analysis                | 20 min    |
| `security_triage`      | Incident detected, needs analysis             | 30 min    |
| `security_remediation` | Researched incident, needs a code fix         | 90 min    |

## Step 3 — Write rolling summary (turn 2+ only)

Write a complete summary of the entire conversation so far — every message sent and every reply received — before continuing. The receiving mind wakes in a fresh session with no memory of prior turns. The rolling summary is its only history. Be tight and precise — verbose summaries inflate both parties' context costs.

## Step 4 — POST to broker

Use bash/curl to POST to the broker endpoint:

**Inside container:**
```bash
curl -s -X POST http://server:8420/broker/messages \
  -H "Content-Type: application/json" \
  -d '{
    "message_id": "<generated-uuid>",
    "conversation_id": "<uuid>",
    "from": "<your-mind-id>",
    "to": "<target-mind-id>",
    "content": "<the message>",
    "rolling_summary": "<summary or empty string>",
    "metadata": {
      "request_type": "<selected-type>",
      "triggered_by": "user_request",
      "expects_reply": true
    }
  }'
```

**Outside container:**
Replace `http://server:8420` with `http://localhost:8420`.

The broker returns immediately:
```json
{ "status": "dispatched", "conversation_id": "...", "message_id": "..." }
```

If you get `"status": "exists"`, this message was already sent (idempotent retry). Move on.

## Step 5 — Spawn polling agent

Spawn the `poll-task-result` agent as a **background subagent** immediately after POSTing:

```
poll-task-result \
  --conversation_id <uuid> \
  --from_mind <your-mind-id> \
  --to_mind <target-mind-id> \
  --request_type <selected-type>
```

Then exit. The polling agent runs in the background and will deliver the result to your thread when it arrives. Do not wait for it.
