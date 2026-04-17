# Developer Console (Debug Control Room)

> **Status:** Concept. Not yet designed or implemented.
> Motivation: as system complexity grows, Ada's in-context view of the system
> is incomplete at any given moment. This console gives Daniel a shared
> situational picture so both can diagnose faster.

---

## Problem

The system has reached a complexity level where:

- Ada frequently lacks full context during a debugging session (config state,
  running containers, recent errors, what files were recently changed)
- Daniel is largely hands-off, which is a feature, but it means he has no
  ambient visibility into system state — the gap only surfaces during demos
  or incidents
- Common failure modes (e.g. named Docker volumes overwriting bind mounts)
  require Daniel to narrate the state before Ada can act; a live view would
  eliminate that round-trip

---

## Concept

A terminal-themed developer console — think NOC/SOC dashboard — that Ada
can write to during debugging sessions. Not a log viewer. A structured
workspace Ada populates with her active diagnosis, relevant code, and live
state so Daniel can see exactly what Ada sees.

The mental model: Ada is at the keyboard; Daniel is standing behind her
looking at the same screens.

---

## Layout

```
┌─────────────────────────────────────────────────────────────────────┐
│ ASSESSMENT                                              [timestamp]  │
│  Ada's current diagnosis in plain language. What she thinks is       │
│  wrong, what she ruled out, what she's about to try. Scrollable.    │
└─────────────────────────────────────────────────────────────────────┘

┌──────────────────────────┐  ┌──────────────────────────────────────┐
│ CODE / CONFIG            │  │ SYSTEM STATE                         │
│  Verbatim file content   │  │  Container status, volume mounts,    │
│  or diff. File path +    │  │  env vars, recent git log, service   │
│  line numbers shown.     │  │  health. Read-only snapshot.         │
│  Scrollable.             │  │  Scrollable.                         │
└──────────────────────────┘  └──────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ ACTION LOG                                                           │
│  Timestamped list of what Ada has done / is doing. Tool calls,      │
│  commands run, files edited. Append-only. Scrollable.               │
└─────────────────────────────────────────────────────────────────────┘
```

All panels: dark terminal theme, monospace font, green/amber/white on
near-black. Panels are independently scrollable. No auto-scroll lock when
Daniel is actively reading (pause-on-hover or scroll-lock toggle).

---

## Ada-Side Interface

Ada writes to the console via REST endpoints on the gateway (server.py),
the same way all other system state flows:

| Endpoint | Purpose |
|----------|---------|
| `POST /console/assessment` | Overwrite the assessment panel with current diagnosis |
| `POST /console/code` | Push a file/diff block to the code panel (path, content, highlight_lines) |
| `POST /console/state` | Set a key/value entry in the system state panel |
| `POST /console/log` | Append a timestamped entry to the action log |
| `POST /console/clear` | Reset all panels (start of new debug session) |
| `GET /console` | WebSocket or SSE feed — consumed by the browser frontend |

Ada calls these proactively during any session where something is wrong —
not only on request. Think of it as narrating the work to the room.

---

## Integration Points

- **Gateway endpoints**: console state is held in the gateway (server.py),
  served via WebSocket or SSE to the frontend. Ada writes via HTTP POST,
  same as every other gateway interaction.
- **Website page**: new `/console` route alongside the existing graph and
  canvas pages. Same dark aesthetic.
- **Telegram fallback**: if Daniel asks "what's going on" and the console
  is unpopulated, Ada summarises from her current context as normal. The
  console is additive, not a replacement for conversational updates.

---

## Scope Boundaries

- **Read-only for Daniel.** No input surface in the console itself —
  Daniel uses Telegram to respond or redirect.
- **Not a log aggregator.** Ada populates this intentionally, not via
  automated log streaming. Signal over noise.
- **Not a monitoring dashboard.** No uptime graphs, no metrics. That is
  a separate concern (see `escalation-design.md` for alerting).
- **Not persistent across sessions** (initially). Each `console.clear()`
  or new debug session starts fresh. Historical sessions can be added later.

---

## Open Questions

1. Should Ada auto-clear the console when she starts a new conversation
   (i.e. on session creation), or leave the last state visible until
   explicitly cleared?
2. Should Daniel be able to annotate panels (sticky notes, highlights)?
   Useful but adds scope — defer until v1 is proven useful.
3. Does console state live purely in-memory on the gateway (reset on restart),
   or get written to SQLite for persistence across restarts?

---

## Why This Matters

The named-volume incident (April 2026 demo) is a good example: Ada had
written the docker-compose incorrectly and didn't know it until the crash.
If the console had been showing the active volume mounts at the time,
Daniel would have spotted it before the demo. The fix takes seconds once
you can see the state — the cost is in the back-and-forth to surface it.
