---
name: sitrep
description: Delivers a military-style system situation report (SITREP). Use when the user asks for "sitrep", "sit rep", "system status", or "how are systems doing". ALWAYS trigger on "sit rep" (two words) - this is a hard rule.
user_invocable: true
---

# SITREP

Deliver a military-style system situation report. Collect data in parallel when possible, then present a tight field report.

## Step 1 - Collect data in parallel

Use `multi_tool_use.parallel` to run the shell probes concurrently via `functions.exec_command`.

Run these commands as separate parallel tasks:

```bash
top -bn1 | grep -E '^(%Cpu|Cpu)' | head -1
uptime
free -h
df -h --output=source,size,used,avail,pcent,target | grep -v tmpfs | grep -v udev | grep -v overlay | grep -v shm
cat /proc/net/dev | grep -v lo | tail -n +3
nvidia-smi --query-gpu=name,utilization.gpu,utilization.memory,memory.used,memory.total,fan.speed,temperature.gpu --format=csv,noheader,nounits 2>/dev/null || echo "NO_GPU"
docker ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null || docker compose ps 2>/dev/null || echo "NO_CONTAINERS"
```

Guidance:

- Prefer `multi_tool_use.parallel` for the initial probes.
- If a command is unavailable, keep going and mark that area as unavailable rather than failing the whole report.
- Treat `NO_GPU` as "omit the GPU section entirely".
- Treat `NO_CONTAINERS` as "container status unavailable"; include the section only if you have real container data.

## Step 2 - Format the SITREP

Present the report using this structure and tone. Replace placeholders with real values.

**Formatting rules - apply throughout:**

- Never mention kernel version, OS, or hostname.
- Replace all GB/GiB/G storage units with "gigs" where practical.
- Write "5 by 5" - never "5x5".

---

**SITREP - HIVE MIND // [DATE] [TIME] LOCAL**

**COMMS:** 5 by 5 - reading you loud and clear.

---

**// CPU //**
- User: `[X]%` | System: `[X]%` | Idle: `[X]%`
- Load: `[1m] / [5m] / [15m]`
- Status: `[ALL SYSTEMS NOMINAL if idle >50%, ELEVATED if idle 20-50%, DEGRADED if idle <20%]`

**// MEMORY //**
- Total: `[X gigs]` | Used: `[X gigs]` | Available: `[X gigs]`
- Status: `[NOMINAL / ELEVATED / CRITICAL based on percent used]`

**// DISK //**
- List each drive as: `[device name only, e.g. sda1]` - `[size gigs]` - `[use%] utilization`
- Example: `sda1` - `916 gigs` - `86% utilization`
- Status: `[NOMINAL if all <80%, ELEVATED if any 80-90%, CRITICAL if any >90%]`

**// NETWORK //**
- List each active interface (non-lo) with received and transmitted totals converted to gigs or megs as appropriate
- Status: `NOMINAL` unless errors or drops are present

**// GPU //**
- If `NO_GPU`, omit this section entirely
- For each GPU: `[name]` - `GPU: [util]% | VRAM: [used]/[total] gigs | Fan: [fan]% | Temp: [temp] degrees C`
- Status: `[NOMINAL if util <80% and temp <80 C, ELEVATED if either 80-90, CRITICAL if either >90]`

**// CONTAINERS //**
| Unit | State | Uptime |
|------|-------|--------|
| [service] | [ONLINE/OFFLINE] | [uptime] |
- Status: `[ALL UNITS NOMINAL if all running, otherwise identify degraded units]`

---

**OVERALL ASSESSMENT:** `[ALL SYSTEMS NOMINAL / identify degraded areas]`

**OUT.**

---

## Tone rules

- Use ALL CAPS for status assessments.
- "5 by 5" means comms are loud and clear.
- Use "ALL SYSTEMS NOMINAL" when everything is healthy.
- Use "DEGRADED" for partial issues and "CRITICAL" for severe issues.
- Keep it tight. This is a field report, not an essay.
- Spell out time units: `hours`, `minutes`.
- End with **OUT.**

## Parsing notes

- CPU percentages usually come from the `top` output.
- Load averages come from `uptime`.
- Memory values come from `free -h`.
- Disk values come from `df -h`; strip `/dev/` and keep the device name only when practical.
- Network counters come from `/proc/net/dev`; report only active non-loopback interfaces.
- Container state can be derived from `docker ps` or `docker compose ps` output if available.
- If one subsection is unavailable, continue and produce the rest of the SITREP.
