---
name: check-reminders
description: Check and fire due one-time reminders. Runs every 15 minutes via scheduler. Use when checking for pending reminders.
---

# Check Reminders

This skill runs every 15 minutes to fire any due one-time reminders.

1. Call `get_due_reminders()` to fetch and delete all overdue reminders.
2. If `count` is 0, stop silently — do not send any message.
3. For each fired reminder, send it as a **voice message** using `send_voice_message()`.
   Speak it naturally: "Hey Daniel, just a reminder — <message>."
