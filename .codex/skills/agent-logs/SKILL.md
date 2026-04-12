---
name: agent-logs
description: Scan system log files for critical entries
---

# Agent Logs Tool

Scan system log files for critical entries (errors, failures, panics, etc.). Tracks read position across calls so only new entries are reported.

## Usage

```bash
/usr/src/app/tools/stateless/agent_logs/venv/bin/python /usr/src/app/tools/stateless/agent_logs/agent_logs.py \
  --log-paths "<comma-separated paths>"
```

## Arguments

- `--log-paths` (optional): Comma-separated list of log files to scan (default: /var/log/syslog,/var/log/kern.log)
- `--pos-file` (optional): Path to the position tracking file (default: .log_agent_positions)

## Output

JSON with:
- `status`: "ok" (no critical entries) or "critical" (entries found)
- `findings`: Object mapping log file paths to arrays of critical lines (only when status is "critical")
- `message`: Human-readable summary (only when status is "ok")
