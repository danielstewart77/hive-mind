# Escalation Design

> **Status:** Not yet designed. Extracted from `llm-messaging-architecture.md` Phase 2C.
> Escalation is a broader concern than just inter-mind messaging.

---

## Scope

This plan should cover escalation across the system — not just inter-mind
communication timeouts, but any situation where automated handling reaches
its limits and needs human intervention or policy-based decisions.

### Categories to address

1. **Inter-mind communication escalation** — a delegated task exceeds its
   time/cost budget. When should the system intervene, and how?
2. **Cost escalation** — a mind or session is consuming excessive tokens
   or API calls. When does the system throttle or halt?
3. **Error escalation** — repeated failures in a mind, tool, or service.
   When does the system stop retrying and surface to Daniel?
4. **Security escalation** — a mind attempts something outside its scope
   or a tool audit flags anomalous behaviour.

### Design questions (from original spec)

These were the original inter-mind escalation questions. They should be
answered in the broader context above:

- How long past the notification threshold before escalation triggers? (Suggested: 2x threshold)
- Is kill automatic or does it require human confirmation via Telegram?
- What should the caller do when a conversation is terminated — retry, give up, or surface to Daniel?
- Should escalation behaviour differ by `request_type`? (A `security_remediation` may warrant a human decision rather than an auto-kill)
- Does escalation apply when the mind is Bob (local Ollama, zero cost)?

### Notes

- Not urgent. The current backstop timeouts in the broker handle the
  immediate case (timed_out status after 4x threshold).
- Design this when the system has more operational experience with
  multi-mind workflows.
