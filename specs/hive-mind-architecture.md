# Hive Mind Architecture Principles

## The Pattern

```
Event → Specification → Tools
```

Every non-trivial operation follows this flow. No exceptions for anything requiring
nuance or interpretation.

---

## What Each Layer Does

### Events
Anything that kicks off work: a user message, a scheduled cron job, a Telegram command,
a webhook. Events are thin — they identify what happened and invoke the appropriate skill.

```python
# scheduler.py — correct
async def _memory_backfill_sweep():
    """Monthly backfill pass. Skill does the reasoning."""
    await _post_gateway("/command", {"text": "/memory-backfill"})
```

### Specifications (markdown)
All logic, nuance, decision criteria, and context lives here. When the behavior needs
to change, you edit a markdown file — not Python code.

```
specs/
  memory-lifecycle.md     ← defines data classes, pruning rules, write procedure
  hive-mind-architecture.md  ← this file
  security.md             ← security constraints
skills/
  memory-backfill/
    SKILL.md              ← step-by-step instructions that reference the spec
```

### Tools (Python)
Atomic CRUD operations only. A tool reads from or writes to a system. It does not
decide *what* to read or *whether* to write — that decision was already made by the
skill reading the spec.

```python
# tools/stateless/notify/notify.py — correct: pure utility
def send_telegram(chat_id: str, text: str) -> str:
    """POST to Telegram sendMessage. No retry logic, no formatting decisions."""
    # just sends — no decision about who to notify or when
    ...

# tools/stateless/notify/notify.py — WRONG: logic embedded in tool
def send_telegram(chat_id: str, text: str) -> str:
    if "urgent" in text.lower():                       # ← anti-pattern
        text = f"🚨 URGENT: {text}"                     # ← anti-pattern
    if _is_quiet_hours():                              # ← anti-pattern
        _queue_for_morning(chat_id, text)              # ← anti-pattern
    ...
```

Lucent reads/writes live on the shared `hive_nervous_system` container and
are reached over HTTP+bearer at `LUCENT_URL` — no in-repo Python module
fronts them.

---

## Concrete Example: Memory Backfill

### Wrong (what we built in PR #9)

```python
# core/backfill_classifier.py  ← this file should not exist
DATA_CLASS_KEYWORDS = {
    "technical-config": ["file path", "function", "config key", "module"],
    "person": ["works at", "met", "email", "phone"],
    "session-log": ["session", "today", "morning", "discussed"],
    ...
}

def classify_entry(content: str, tags: str) -> tuple[str, float]:
    scores = {}
    for cls, keywords in DATA_CLASS_KEYWORDS.items():
        scores[cls] = sum(kw in content.lower() for kw in keywords)
    best = max(scores, key=scores.get)
    confidence = scores[best] / len(DATA_CLASS_KEYWORDS[best])
    return best, confidence
```

This is wrong because:
- The keyword list is incomplete and will drift from the spec
- Confidence scoring is arbitrary
- Adding a new data class requires a code change, a PR, a review cycle
- The "reasoning" is a blunt instrument that misclassifies edge cases

### Right

```markdown
# skills/memory-backfill/SKILL.md

## Purpose
Classify all existing memory entries that lack a `data_class` field.

## Steps

1. Read `specs/memory-lifecycle.md` — understand the 7 data classes,
   their definitions, and the Tier model.

2. Call `memory_retrieve(query="unclassified", k=50, tag_filter="unclassified")`
   to fetch entries without a data_class.

3. For each entry, reason about its content against the class definitions
   in the spec. Ask: which class best describes what this entry *is*?

4. High-confidence classification:
   - Call `memory_store(... data_class=<determined_class>)` to update the entry.

5. Uncertain or ambiguous entries:
   - Batch them. Call `notify_owner` with a grouped summary:
     "I'm unsure how to classify these entries. Please review:
      [entry summary] — candidates: [class A], [class B]"

6. After the pass, call `notify_owner` with a summary:
   "Backfill complete. Classified: N. Flagged for review: M."
```

The tools called are exactly the same (`memory_retrieve`, `memory_store`,
`notify_owner`) — but the reasoning lives in the skill reading the spec, not
in Python heuristics.

---

## When to Use a Skill vs. a Tool

| Situation | Use |
|-----------|-----|
| Requires reading context or nuance | Skill (reads spec) |
| Requires deciding between options | Skill (reads spec) |
| Behavior might need to change | Skill/spec (edit markdown, not code) |
| Pure read/write to a system | Tool (skill or HTTP service call) |
| Date arithmetic, string formatting | Tool or inline code |
| Sending a notification | Tool (`notify_owner`) |
| Classifying, interpreting, reasoning | Skill (NEVER a tool) |

---

## Story Requirements

When writing Planka stories for Hive Mind development, every story that involves
logic or reasoning must include:

1. **The skill file path** — `skills/<name>/SKILL.md`
2. **The spec file(s) it reads** — explicit references
3. **The tools it calls** — listed as atomic operations
4. **A concrete example** showing the event→spec→tools flow
5. **What must NOT be in Python code** — call this out explicitly

---

## What Lives Where

| What | Where | Format |
|------|-------|--------|
| Data class definitions | `specs/memory-lifecycle.md` | Markdown |
| Pruning rules | `specs/memory-lifecycle.md` | Markdown |
| Security constraints | `specs/security.md` | Markdown |
| Step-by-step job logic | `skills/<name>/SKILL.md` | Markdown |
| Scheduled job triggers | `clients/scheduler.py` | Python (thin) |
| Lucent read/write | `hive_nervous_system` HTTP API (`LUCENT_URL`) | External service |
| Telegram send | `tools/stateless/notify/notify.py` | Python (CRUD only) |
| Graph read/write | `hive_nervous_system` HTTP API (`LUCENT_URL`) | External service |
| Classification logic | ❌ NOT in Python | Skill reads spec |
| Heuristics | ❌ NOT in Python | Skill reads spec |
| Decision trees | ❌ NOT in Python | Skill reads spec |
