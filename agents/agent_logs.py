import os
import re
import json
from typing import Optional
from agent_tooling import tool
from utilities.openai_tools import completions_streaming


# Default log files to monitor
DEFAULT_LOG_PATHS = ["/var/log/syslog", "/var/log/kern.log"]

# Regex patterns to detect critical log entries
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


def read_new_log_lines(logfile: str, last_position: int) -> tuple[int, list[str]]:
    """Read new lines from the logfile starting at last_position."""
    if not os.path.exists(logfile):
        return last_position, []

    with open(logfile, 'r') as f:
        f.seek(last_position)
        lines = f.readlines()
        last_position = f.tell()
    return last_position, lines


def find_critical_lines(lines: list[str]) -> list[str]:
    """Return a list of log lines that match critical patterns."""
    critical_lines = []
    for line in lines:
        for pattern in CRITICAL_PATTERNS:
            if pattern.search(line):
                critical_lines.append(line.strip())
                break
    return critical_lines


@tool(tags=["system"])
def agent_logs(messages: Optional[list[dict[str, str]]] = None, log_paths: Optional[list[str]] = None) -> str:
    """
    Call this function any time the user specifically mentions logs or log files.
    """
    # Parse messages if passed as a string (from JSON)
    if isinstance(messages, str):
        try:
            parsed = json.loads(messages)
            if isinstance(parsed, list):
                messages = parsed
            else:
                messages = None
        except Exception:
            messages = None

    # Default log paths
    if not log_paths or not all(isinstance(p, str) for p in log_paths):
        log_paths = DEFAULT_LOG_PATHS

    positions = {}
    pos_file = ".log_agent_positions"

    # Load saved log positions
    if os.path.exists(pos_file):
        with open(pos_file, 'r') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) == 2:
                    try:
                        positions[parts[0]] = int(parts[1])
                    except ValueError:
                        positions[parts[0]] = 0

    all_critical = []

    for logfile in log_paths:
        if os.path.isdir(logfile):
            print(f"[agent_logs] Skipping directory: {logfile}")
            continue  # Skip directories

        last_pos = positions.get(logfile, 0)
        last_pos, new_lines = read_new_log_lines(logfile, last_pos)
        positions[logfile] = last_pos

        critical_lines = find_critical_lines(new_lines)
        if critical_lines:
            all_critical.append((logfile, critical_lines))

    # Save updated positions
    with open(pos_file, 'w') as f:
        for logfile, pos in positions.items():
            f.write(f"{logfile}|{pos}\n")

    if not all_critical:
        # Return a message if no critical logs were found
        message = "No critical action items detected in the monitored logs."
        stream = completions_streaming(message=message)
        full_response = ""
        for chunk in stream:
            full_response += chunk
        return full_response

    # Compose a report
    report_lines = ["Critical action items detected:"]
    for logfile, lines in all_critical:
        report_lines.append(f"\n=== {logfile} ===")
        for line in lines:
            report_lines.append(f"- {line}")

    report_str = "\n".join(report_lines)

    # Format and return the report
    stream = completions_streaming(message=f"Format this message nicely for the user:\n{report_str}")
    full_response = ""
    for chunk in stream:
        full_response += chunk
    return full_response