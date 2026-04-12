# GNOME Shell Stability — Diagnosis & Mitigation Plan

> **Status:** Diagnosed. Mitigation steps not yet implemented.
> **Triggered by:** UI freeze on 2026-04-10 after 21 days uptime.

---

## Incident Summary

GNOME Shell (PID 9274) locked up the desktop after running for 21 days.
The shell process was still alive (D-Bus responded) but the compositor
render loop was stuck — no frames reached the display. A `killall -HUP
gnome-shell` restored the UI without losing terminal sessions.

## Environment

| Item | Value |
|------|-------|
| OS | Ubuntu 24.04.4 LTS (Wayland session) |
| GPU | NVIDIA RTX A6000 |
| NVIDIA Driver | 570.211.01 |
| Desktop | GNOME Shell on Mutter (Wayland) |
| Uptime at freeze | 21 days |

## Root Cause Analysis

### Primary: Mutter/NVIDIA compositor deadlock

The kernel logged `[nvidia-drm] Flip event timeout on head 0` — the NVIDIA
driver gave up waiting for Mutter to complete a page flip. The compositor's
render loop stalled, but the shell process stayed alive (D-Bus calls worked,
extensions kept running).

### Contributing Factor 1: Window stack corruption (Mutter bug)

47 instances of `meta_window_set_stack_position_no_sync: assertion
'window->stack_position >= 0' failed` accumulated over 21 days. This is a
known Mutter bug where Wayland clients creating/destroying surfaces
improperly corrupt the compositor's internal window stack. Telegram Desktop
was actively triggering bad popup parenting at the time.

Each failed assertion further corrupts internal state. Over 21 days this
accumulated until the compositor's frame dispatch stalled.

### Contributing Factor 2: Memory leak

gnome-shell peaked at 1.7GB (typical is 500–800MB for multi-day sessions).
CPU consumption was ~8 hours total vs ~2 hours for normal multi-day sessions.
The main loop was working significantly harder than normal.

### Contributing Factor 3: Zombie process accumulation

32 zombie processes — orphaned chrome-headless and python processes from
browser automation (Playwright). These leaked Wayland surface references
and file descriptors into gnome-shell's process table (191 open FDs at
time of investigation).

### Contributing Factor 4: Extension overhead

- **ding@rastersoft.com** — runs as a GJS subprocess, aggressively
  re-registers keybindings on startup. Known source of GJS memory leaks
  over long uptimes.
- **claude-usage@local** — polls every 2 minutes, adding GJS timer and
  allocation churn to the shell's main loop.
- **gnome-software** (755MB, running 21 days) — repeatedly failing app
  filter reloads, adding D-Bus noise.

### Contributing Factor 5: NVIDIA driver version

570.211.01 has known Wayland flip-timeout regressions. Later versions
(575+) include fixes for this class of issue.

## Observed Symptoms

- Desktop completely unresponsive (no mouse, no keyboard in GUI)
- Terminal sessions via SSH/Claude Code still functional
- gnome-shell process alive, responding to D-Bus
- No OOM kills, no segfaults, no kernel panics
- Load average normal (0.30)

## Recovery Procedure

```
# Step 1: Verify gnome-shell is the issue (should return a result)
gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell \
  --method org.gnome.Shell.Eval 'global.get_current_time()'

# Step 2: Restart gnome-shell (safe on Wayland, preserves terminal sessions)
killall -HUP gnome-shell

# Step 3: If HUP doesn't work, hard restart GDM (kills GUI sessions)
sudo systemctl restart gdm
```

## Mitigation Options (to discuss)

### 1. Periodic gnome-shell restart

Restart gnome-shell on a schedule to prevent the 21-day accumulation.
Could be a cron job or systemd timer running `killall -HUP gnome-shell`.

**Open questions:**
- What frequency? Weekly? Bi-weekly?
- Should it be automatic (cron) or a reminder to do it manually?
- What time of day minimizes disruption?
- Does HUP reliably restart without issues every time, or are there edge cases?

### 2. Zombie process reaping

Add cleanup for orphaned chrome-headless/python processes from browser
automation (Playwright in hive_mind containers).

**Open questions:**
- Should the hive_mind container be responsible for cleaning up its own
  Playwright processes?
- Or should a host-level cron job reap zombies periodically?
- Can we fix the root cause in the browser automation tool to properly
  terminate child processes?

### 3. gnome-software

Disable gnome-software autostart — it was using 755MB doing nothing useful.
`systemctl --user mask gnome-software-service.service`

**Open questions:**
- Does Daniel use gnome-software for anything? Or is apt/snap CLI sufficient?
- Any downside to masking it?

### 4. claude-usage extension polling interval

Currently polls every 2 minutes. Reducing to 10–15 minutes would lower
GJS timer churn.

**Open questions:**
- Where is the polling interval configured?
- What does the extension actually display / is it worth keeping?

### 5. ding@rastersoft.com extension

Most problematic extension for long-running sessions. Provides desktop
icons functionality.

**Open questions:**
- Does Daniel use desktop icons?
- Is there a lighter alternative?
- Or is disabling it acceptable?

### 6. NVIDIA driver update

Upgrade from 570.211.01 to 575+ for Wayland flip-timeout fixes.

**Open questions:**
- Is 575 available in the Ubuntu 24.04 repos or does it need a PPA?
- Any risk of breaking CUDA/Docker GPU passthrough?
- Should this wait for the system migration (new 2TB drive)?

### 7. Telegram Desktop Wayland behavior

Telegram was triggering bad popup parenting that contributed to the
Mutter stack corruption.

**Open questions:**
- Is this the snap version or a .deb?
- Can it be run in XWayland mode to avoid the Wayland surface bugs?
- Is there an update that fixes the popup behavior?
