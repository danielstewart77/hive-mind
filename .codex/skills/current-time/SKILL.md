---
name: current-time
description: Get the current date and time for any timezone
---

# Current Time Tool

Get the current date and time with day of week.

## Usage

Run the current-time tool via Bash:

```bash
/usr/src/app/tools/stateless/current_time/venv/bin/python /usr/src/app/tools/stateless/current_time/current_time.py \
  --timezone "<IANA timezone>"
```

## Arguments

- `--timezone` (optional): IANA timezone string (default: "America/Chicago"). Examples: "UTC", "Europe/London", "Asia/Tokyo".

## Output

JSON object with:
- `time`: Formatted date/time string (e.g. "Monday, March 10, 2026 at 3:45 PM CDT")
- `timezone`: The timezone used
