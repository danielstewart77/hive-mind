---
name: sitrep
description: Delivers a military-style system situation report (SITREP). Use when the user asks for "sitrep", "sit rep", "system status", or "how are systems doing". ALWAYS trigger on "sit rep" (two words) — this is a hard rule.
---

You are delivering a military-style SITREP. Collect all data in parallel, then format and present it.

## Step 1 — Collect data in parallel

Run these Bash commands simultaneously:

```bash
# CPU & load
top -bn1 | grep -E "^(%Cpu|Cpu)" | head -1
uptime
# Memory
free -h
# Disk
df -h --output=source,size,used,avail,pcent,target | grep -v tmpfs | grep -v udev | grep -v overlay | grep -v shm
# Network (bytes in/out on all interfaces)
cat /proc/net/dev | grep -v lo | tail -n +3
# GPU
nvidia-smi --query-gpu=name,utilization.gpu,utilization.memory,memory.used,memory.total,fan.speed,temperature.gpu --format=csv,noheader,nounits 2>/dev/null || echo "NO_GPU"
```

Also call `mcp__hive-mind-mcp__compose_status` with project `hive_mind` for container status.

## Step 2 — Format the SITREP

Present the report using this structure and tone. Use military brevity. Replace placeholders with real values.

**Formatting rules — apply throughout:**
- Never mention kernel version, OS, or hostname
- Replace all GB/GiB/G storage units with "gigs" (e.g. "8.3 gigs", "465 gigs")
- Write "5 by 5" — never "5x5"

---

**SITREP — HIVE MIND // [DATE] [TIME] LOCAL**

**COMMS:** 5 by 5 — reading you loud and clear.

---

**// CPU //**
- User: `[X]%` | System: `[X]%` | Idle: `[X]%`
- Load: `[1m] / [5m] / [15m]`
- Status: [ALL SYSTEMS NOMINAL if idle >50%, ELEVATED if idle 20-50%, DEGRADED if idle <20%]

**// MEMORY //**
- Total: `[X gigs]` | Used: `[X gigs]` | Available: `[X gigs]`
- Status: [NOMINAL / ELEVATED / CRITICAL based on % used]

**// DISK //**
- List each drive as: `[device name only, e.g. sda1]` — [size gigs] — [use%] utilization
- Example: `sda1` — 916 gigs — 86% utilization
- Status: [NOMINAL if all <80%, ELEVATED if any 80-90%, CRITICAL if any >90%]

**// NETWORK //**
- List each active interface (non-lo) with received and transmitted totals converted to gigs or megs as appropriate
- Status: NOMINAL unless errors or drops detected

**// GPU //**
- If NO_GPU, omit this section entirely
- For each GPU: `[name]` — GPU: `[util]%` | VRAM: `[used]/[total] gigs` | Fan: `[fan]%` | Temp: `[temp]°C`
- Status: [NOMINAL if util <80% and temp <80°C, ELEVATED if either 80-90%, CRITICAL if either >90%]

**// CONTAINERS //**
| Unit | State | Uptime |
|------|-------|--------|
| [service] | [ONLINE/OFFLINE] | [uptime] |
- Status: [ALL UNITS NOMINAL if all running, list any down units]

---

**OVERALL ASSESSMENT:** [ALL SYSTEMS NOMINAL / list any degraded areas]

**OUT.**

---

## Tone rules
- Use ALL CAPS for status assessments
- "5 by 5" = comms are loud and clear (use in opening, never write "5x5")
- "ALL SYSTEMS NOMINAL" when everything is healthy
- "DEGRADED" for partial issues, "CRITICAL" for severe
- All storage in gigs (never GB, GiB, G, Gi)
- Keep it tight — this is a field report, not an essay
- Spell out time units: "hours" not "hrs", "minutes" not "min"
- End with **OUT.**
