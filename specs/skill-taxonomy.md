# Skill Taxonomy — Broad Category Skills

## Problem

The skill list currently contains 30+ narrow, action-specific skills (e.g. `verify-capability`, `planning-genius`, `send-email`). The UserPromptSubmit hook reminds Ada to check for relevant skills before acting, but narrow skills only get matched when the conversational phrasing closely resembles the skill name. Loosely-worded questions ("what would we need for X?", "how does Y work?") can bypass narrow skills even when a procedure exists that should govern the response.

## Proposed Solution

A small taxonomy of **broad category skills** — analogous to the data classification index — where every type of conversation maps clearly to one category. Each category skill contains the procedural checklist for how to approach that class of work, including guardrails like "read the code before designing" and "check specs before building."

The key insight from data classification: fewer, broader categories are more robust than many specific ones. If the category name is broad enough, any phrasing of a request in that domain will trigger recognition.

## Taxonomy (Draft)

| Skill Name | Covers |
|---|---|
| **engineering** | code, architecture, capabilities, "what would we need", "how does X work", system design, specs, debugging, codebase questions |
| **operations** | deployments, container restarts, config changes, infrastructure, services |
| **communication** | emails, messages, Telegram, LinkedIn, notifications, anything sent to a person |
| **planning** | stories, tasks, Planka, calendar events, scheduling, project decisions |
| **information** | research, lookups, news briefings, "what is", "who is", reading external content |

Five categories. Every conversation Daniel and Ada have falls into one of these.

## Skill Structure

Each broad skill follows this structure:

### Step 1 — Announce

**The first action in every broad skill is to announce its invocation to the user.**

Example: *"Using: engineering skill."*

This gives both Ada and Daniel visibility into whether the hook + skill matching is working. If the wrong skill fires, Daniel can correct it. If no skill fires when one should have, that's signal the taxonomy needs refinement.

### Step 2 — Category-Specific Procedure

Each skill runs its checklist. The **engineering** skill is described in full below as the reference implementation. Other category skills follow the same pattern.

---

## Engineering Skill (Reference Design)

The engineering skill is a **lifecycle router**. It determines where in the engineering process a request falls, then delegates to the appropriate sub-skill. Skills load lazily — the engineering skill stays lean and routes; it does not duplicate what sub-skills do.

### Step 1 — Announce

> *"Using: engineering."*

### Step 2 — Assess (always first, no exceptions)

Read before acting. Every engineering request starts here:

1. Search `specs/` for any spec file related to the topic
2. Search the codebase (`grep`, `glob`, read relevant files) for existing implementation
3. Establish ground truth: what exists, what is specced, what is neither

**This step is mandatory.** Designing or speccing before completing Step 2 is the failure mode this skill exists to prevent.

### Step 3 — Route Based on State

Based on what Step 2 found, take one of these paths:

| State | Action |
|---|---|
| **Already fully implemented** | Explain what exists and where. No further action unless the request is to extend or debug it. |
| **Informational question** ("how does X work", "what does Y do") | Answer from code/spec reading. Done. |
| **Bug or extension needed** | Use `/planning-genius` if non-trivial, or `/code-genius` for straightforward fixes. |
| **Spec exists, no Planka story yet** | Use `/create-story` to capture it, then `/story-start` to begin. |
| **Story in progress** | Use `/story-start` (if beginning work) or `/code-genius` (if mid-implementation). |
| **Code written, needs review** | Use `/code-review-genius`, then `/commit` when approved, then `/story-close`. |
| **Net new idea, nothing exists** | Discussion mode — no tools yet. Design conversationally with Daniel. When aligned, write a spec to `specs/`. Then loop back to "spec exists, no story yet." |

### Step 4 — Full Story Lifecycle (when building)

When a request moves from idea to implementation, the sub-skill chain is:

```
/create-story → /story-start → /planning-genius → /code-genius → /code-review-genius → /commit → /story-close
```

The engineering skill does not execute these — it identifies which step in this chain applies and invokes the right one.

### Notes on Sub-Skill Delegation

- Sub-skills load on demand. The engineering skill references them by name; their content loads only when invoked.
- If multiple sub-skills are needed in sequence, invoke them in order — do not try to do their work inline.
- The engineering skill is the decision tree. Sub-skills are the executors.

## Feedback Loop

The announcement in Step 1 creates a visible feedback signal. Over time:
- If a skill announces itself and Daniel confirms the work went well → the category match is working
- If a skill announces itself for the wrong reason → taxonomy name or description needs adjustment
- If Ada acts without announcing a skill when she should have → the hook + taxonomy missed a phrasing pattern

This visibility is how we tune the taxonomy without instrumenting anything else.

## Open Questions

1. **Naming**: Should category skills be named generically (`engineering`) or more descriptively (`codebase-work`, `system-design`)? Generic names are easier to trigger; descriptive names are clearer in announcements.
2. **Overlap handling**: Some requests touch multiple categories (e.g. "send an email about this architecture decision"). Does Ada pick the primary category or chain skills?
3. **Existing narrow skills**: Broad skills wrap narrow ones. Engineering fires first (assess + route), then delegates to `planning-genius`, `code-genius`, etc. for execution. Narrow skills are not replaced — they become the sub-skill layer.
4. **Announcement format**: How visible should the announcement be? A brief inline note ("Using: engineering.") vs. a more explicit statement.

## Next Steps

1. Finalize the 5-category taxonomy with Daniel
2. Create each skill file with the announce-first structure
3. Run for a few sessions, observe what fires and what doesn't
4. Refine category names and trigger descriptions based on what gets missed
