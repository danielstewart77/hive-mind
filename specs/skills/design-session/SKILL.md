---
name: design-session
description: Multi-turn design and planning session. Use when Daniel is thinking through an architecture, designing a new system, working on a plan, or iterating on ideas over multiple turns. Governs file placement, the write-on-consensus procedure, and canvas linking.
user-invocable: true
---

# Design Session

A multi-turn working session where an idea is explored, refined, and recorded. This skill governs where things get written and how the conversation connects to the file system.

---

## File Taxonomy

Before writing anything, place it in the right folder:

| Type | Location | Description |
|------|----------|-------------|
| **Plan** | `plans/` | Active design work — current vs future, ideas being developed, architecture exploration. This is where we write during a design session. |
| **Spec** | `specs/` | Documents actively referenced and executed by a skill or procedure. If no skill points to it, it is not a spec — move it to `docs/`. |
| **Documentation** | `docs/` | Informational reference. Not procedurally executed. Referenced from README at the appropriate level. |

**Default during a design session: write to `plans/`.**

---

## Identifying the Active Plan File

At the start of (or during) a design session:

1. Ask Daniel which plan file we're working on, or check `plans/` for an existing file that matches the topic.
2. Confirm the file path once. That file is the **active plan** for the session.
3. Do not switch files mid-session without explicit instruction.

If no plan file exists yet, create one: `plans/<topic-name>.md` — kebab-case, descriptive.

---

## Write-on-Consensus Procedure

This is the loop for every design turn:

1. **Idea surfaces** — Daniel raises it, or Ada surfaces a design question.
2. **Present the idea** — Ada describes it clearly in conversation.
3. **Consensus reached** — Daniel confirms, approves, or adjusts.
4. **Write to the plan file** — immediately, before moving to the next topic.
5. **Continue** — next idea.

> Do not batch writes. Record each agreed point as it is settled. If the session ends before a point is written, it is lost.

---

## Canvas Rule

The canvas is a **live view of the plan file** — not independent content.

- Never write to the canvas directly.
- Never display content on the canvas that is not already in the plan file.
- To put the session on the canvas: add a link to the plan file in `canvas.md`. After that, writing to the plan file automatically updates the canvas view.
- If Daniel asks to "show this on the canvas," write it to the plan file first, then link the file on the canvas.

---

## What This Session Is Not

- Not a coding session — no implementation until the plan is settled.
- Not a spec update session — do not modify existing `specs/` files unless Daniel explicitly asks to update a spec. Design work lives in `plans/`.
- Not a one-shot command — this skill governs an ongoing conversation, not a single action.

---

## Triggers

Invoke this skill (or stay in its mode) when Daniel says things like:

- "Let's work through this idea..."
- "I'm thinking about how X should work..."
- "Let's design / redesign X"
- "What are your thoughts on the architecture for..."
- "I'm reading through the plan / spec / design..."
- "Let's figure out how to approach..."
- Any multi-turn exploration of a system, feature, or architecture
