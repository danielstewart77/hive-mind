#!/usr/bin/env python3
"""Scan system log files for critical entries (errors, failures, panics).

Standalone stateless tool. No external dependencies.
Tracks read position across calls so only new entries are reported.
"""

import argparse
import json
import os
import re
import sys

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

_DEFAULT_POS_FILE = ".log_agent_positions"


def _load_positions(pos_file: str) -> dict[str, int]:
    """Load last-read positions from the position tracking file."""
    positions: dict[str, int] = {}
    if os.path.exists(pos_file):
        with open(pos_file, "r") as f:
            for line in f:
                parts = line.strip().split("|")
                if len(parts) == 2:
                    try:
                        positions[parts[0]] = int(parts[1])
                    except ValueError:
                        positions[parts[0]] = 0
    return positions


def _save_positions(positions: dict[str, int], pos_file: str) -> None:
    """Save current read positions to the position tracking file."""
    os.makedirs(os.path.dirname(pos_file) or ".", exist_ok=True)
    with open(pos_file, "w") as f:
        for logfile, pos in positions.items():
            f.write(f"{logfile}|{pos}\n")


def scan_logs(log_paths: list[str], pos_file: str) -> dict:
    """Scan log files for critical entries since last read position.

    Args:
        log_paths: List of log file paths to scan.
        pos_file: Path to the position tracking file.

    Returns:
        Dict with status and findings.
    """
    positions = _load_positions(pos_file)
    results: dict[str, list[str]] = {}

    for logfile in log_paths:
        if not os.path.isfile(logfile):
            continue

        last_pos = positions.get(logfile, 0)
        with open(logfile, "r") as f:
            f.seek(last_pos)
            lines = f.readlines()
            positions[logfile] = f.tell()

        critical: list[str] = []
        for line in lines:
            for pattern in CRITICAL_PATTERNS:
                if pattern.search(line):
                    critical.append(line.strip())
                    break

        if critical:
            results[logfile] = critical

    _save_positions(positions, pos_file)

    if not results:
        return {"status": "ok", "message": "No critical entries found in monitored logs."}

    return {"status": "critical", "findings": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan system logs for critical entries")
    parser.add_argument(
        "--log-paths",
        default=",".join(DEFAULT_LOG_PATHS),
        help="Comma-separated list of log files to scan",
    )
    parser.add_argument(
        "--pos-file",
        default=_DEFAULT_POS_FILE,
        help="Path to position tracking file",
    )
    args = parser.parse_args()

    log_paths = [p.strip() for p in args.log_paths.split(",") if p.strip()]
    result = scan_logs(log_paths, args.pos_file)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
