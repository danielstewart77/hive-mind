---
name: reminders
description: Set, list, delete, and check due one-time reminders
user-invocable: false
---

# Reminders Tool

Manage one-time reminders backed by SQLite with natural language time parsing.

## Usage

### Set a reminder
```bash
/usr/src/app/tools/stateless/reminders/venv/bin/python /usr/src/app/tools/stateless/reminders/reminders.py set \
  --message "<reminder text>" \
  --when "<time expression>"
```

### List pending reminders
```bash
/usr/src/app/tools/stateless/reminders/venv/bin/python /usr/src/app/tools/stateless/reminders/reminders.py list
```

### Delete a reminder
```bash
/usr/src/app/tools/stateless/reminders/venv/bin/python /usr/src/app/tools/stateless/reminders/reminders.py delete \
  --reminder-id <id>
```

### Get and fire due reminders
```bash
/usr/src/app/tools/stateless/reminders/venv/bin/python /usr/src/app/tools/stateless/reminders/reminders.py due
```

## Arguments

### set
- `--message` (required): Reminder message text
- `--when` (required): When to fire (e.g. "2026-03-01 14:30", "tomorrow at 9am", "in 2 hours")
- `--db-path` (optional): SQLite database path (default: /usr/src/app/data/reminders.db)

### list
- `--db-path` (optional): SQLite database path

### delete
- `--reminder-id` (required): Integer ID of the reminder to delete
- `--db-path` (optional): SQLite database path

### due
- `--db-path` (optional): SQLite database path

## Output

JSON with operation results. Set returns reminder ID and fire time. List returns array of pending reminders. Due returns fired reminders and removes them from the database.
