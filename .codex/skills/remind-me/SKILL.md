---
name: remind-me
description: Reads daily, weekly, and backlog reminder files and presents items based on the current day.
---

# Remind Me

Read reminders from `/usr/src/app/reminders/` and present them based on the current date.

## Step 1 — Check the date

Use `get_current_time` (or read the timestamp on the message) to determine:
- Day of week (is it Monday?)
- Day of month (is it the 1st?)

## Step 2 — Daily reminders

Read `/usr/src/app/reminders/daily.md`. If it contains any bullet items, read them out as reminders. If the file is empty or missing, skip silently.

## Step 3 — Weekly reminders (Monday only)

If today is Monday, read `/usr/src/app/reminders/weekly.md`. If it contains any bullet items, read them out. Skip silently otherwise.

## Step 4 — Backlog prompt (1st of the month only)

If today is the 1st of the month, ask: "You have backlog items. Would you like to review them now?" Do not read the backlog contents — just prompt. If Daniel says yes, read `/usr/src/app/reminders/backlog.md`.
