import json
import os
import re
from agent_tooling import tool


DEFAULT_LOG_PATHS = ["/var/log/syslog", "/var/log/kern.log"]

CRITICAL_PATTERNS = [
    re.compile(r"\bcrit(ical)?\b", re.IGNORECASE),
    re.compile(r"\berror\b", re.IGNORECASE),
    re.compile(r"\bfail(ed|ure)?\b", re.IGNORECASE),
    re.compile(r"\balert\b", re.IGNORECASE),
    re.compile(r"\bemergenc(y)?\b", re.IGNORECASE),
    re.compile(r"\bpanic\b", re.IGNORECASE),
    re.compile(r"\bdenied\b", re.IGNORECASE),
    re.compile(r"\bsegmentation fault\b", re.IGNORECASE),
]

POS_FILE = ".log_agent_positions"


def _load_positions() -> dict[str, int]:
    positions = {}
    if os.path.exists(POS_FILE):
        with open(POS_FILE, "r") as f:
            for line in f:
                parts = line.strip().split("|")
                if len(parts) == 2:
                    try:
                        positions[parts[0]] = int(parts[1])
                    except ValueError:
                        positions[parts[0]] = 0
    return positions


def _save_positions(positions: dict[str, int]):
    with open(POS_FILE, "w") as f:
        for logfile, pos in positions.items():
            f.write(f"{logfile}|{pos}\n")


@tool(tags=["system"])
def agent_logs(log_paths: list[str] = None) -> str:
    """Scan system log files for critical entries (errors, failures, panics, etc.).

    Tracks read position across calls so only new entries are reported.
    Returns raw JSON with critical log lines grouped by file.

    Args:
        log_paths: Log files to scan. Defaults to /var/log/syslog and /var/log/kern.log.
    """
    if not log_paths:
        log_paths = DEFAULT_LOG_PATHS

    positions = _load_positions()
    results = {}

    for logfile in log_paths:
        if not os.path.isfile(logfile):
            continue

        last_pos = positions.get(logfile, 0)
        with open(logfile, "r") as f:
            f.seek(last_pos)
            lines = f.readlines()
            positions[logfile] = f.tell()

        critical = []
        for line in lines:
            for pattern in CRITICAL_PATTERNS:
                if pattern.search(line):
                    critical.append(line.strip())
                    break

        if critical:
            results[logfile] = critical

    _save_positions(positions)

    if not results:
        return json.dumps({"status": "ok", "message": "No critical entries found in monitored logs."})

    return json.dumps({"status": "critical", "findings": results})
