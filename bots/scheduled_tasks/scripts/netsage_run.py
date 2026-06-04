#!/usr/bin/env python3
"""NetSage alert pass — Python-only to avoid codex shell-quoting hazards.

Runs once per scheduler fire. Pulls the last 15 minutes of logs from
Loki, picks out anomalous lines, and on a real finding dispatches a
detailed report to Skippy via the broker. Skippy's event-triage
pipeline owns classification, recommendation, and notifying Daniel —
this script no longer pings Daniel directly so there's only one
source of NetSage notifications.
Prints a one-line status to stdout for the scheduling mind to echo back.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid

LOKI_URL = "http://sentinel-loki:3100/loki/api/v1/query_range"
SKIPPY_MIND_ID = "14cb820b-4a42-4f04-a593-54f532fd1d2f"
BILBY_MIND_ID = "37cd48f9-1ed5-4875-91c1-a3b0464deafc"

ANOMALY_PATTERNS = ("error", "critical", "panic", "traceback", "denied", "fatal")
ANOMALY_REGEX = re.compile(
    r"\b(?:" + "|".join(ANOMALY_PATTERNS) + r")\b", re.IGNORECASE
)
MAX_JOURNAL_PRIORITY = 4
BENIGN_SUBSTRINGS = (
    "auto_remember",
    "scheduled_skill",
    "heartbeat",
    "GET /healthz",
    "GET /metrics",
    "scheduler tick",
    "cron fired",
)


def pull_loki_lines() -> list[tuple[str, str, str]]:
    end_ns = time.time_ns()
    start_ns = end_ns - 15 * 60 * 1_000_000_000
    query = urllib.parse.urlencode({
        "query": '{service_name=~".+"}',
        "start": start_ns,
        "end": end_ns,
        "limit": 2000,
        "direction": "backward",
    })
    try:
        with urllib.request.urlopen(f"{LOKI_URL}?{query}", timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"Loki query failed: {exc}")
        return []
    lines: list[tuple[str, str, str]] = []
    for stream in data.get("data", {}).get("result", []):
        meta = stream.get("stream", {})
        svc = (
            meta.get("compose_service")
            or meta.get("container_name")
            or meta.get("service_name")
            or meta.get("container")
            or "?"
        )
        if isinstance(svc, str):
            svc = svc.lstrip("/")
        for entry in stream.get("values", []):
            ts, msg = entry[0], entry[1]
            lines.append((svc, ts, msg))
    return lines


def _decode_envelope(msg: str) -> dict | None:
    """Return the parsed dict if msg is a JSON log envelope, else None."""
    stripped = msg.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        record = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    return record


def _journal_priority(record: dict | None) -> int | None:
    if not record:
        return None
    raw = record.get("PRIORITY")
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _inner_message(record: dict | None, raw_msg: str) -> str:
    """Unwrap one level of JSON envelope so downstream sees the real log line.

    Vector ships docker container logs as a JSON blob with the original line
    in a 'message' field. Journald entries put it in MESSAGE. If neither is
    present, fall back to the raw line so we never lose signal.
    """
    if not record:
        return raw_msg
    for key in ("message", "MESSAGE", "log", "msg"):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return raw_msg


def _service_label(record: dict | None, raw_svc: str) -> str:
    """Prefer the human-readable container name over a hex container id."""
    if record:
        for key in ("container_name", "CONTAINER_NAME", "service_name", "image_name"):
            val = record.get(key)
            if isinstance(val, str) and val.strip():
                return val.lstrip("/")
    return raw_svc


def pick_anomalies(lines):
    out = []
    for svc, ts, msg in lines:
        record = _decode_envelope(msg)
        priority = _journal_priority(record)
        if priority is not None and priority > MAX_JOURNAL_PRIORITY:
            continue
        inner = _inner_message(record, msg)
        if not ANOMALY_REGEX.search(inner):
            continue
        low = inner.lower()
        if any(b.lower() in low for b in BENIGN_SUBSTRINGS):
            continue
        label = _service_label(record, svc)
        out.append((label, ts, inner))
    return out


def broker_skippy(detail: str) -> None:
    broker_url = os.environ.get("HIVEMIND_BROKER_URL")
    broker_token = os.environ.get("HIVEMIND_BROKER_TOKEN")
    if not (broker_url and broker_token):
        print("Broker env missing; skipping Skippy dispatch")
        return
    body = {
        "message_id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "from": BILBY_MIND_ID,
        "to": SKIPPY_MIND_ID,
        "content": detail,
        "rolling_summary": "",
        "metadata": {
            "request_type": "security_triage",
            "triggered_by": "scheduler",
            "expects_reply": False,
        },
    }
    req = urllib.request.Request(
        f"{broker_url}/broker/messages",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {broker_token}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        print(f"Broker dispatch failed: {exc}")


def main() -> None:
    lines = pull_loki_lines()
    anomalies = pick_anomalies(lines)
    if not anomalies:
        print("No anomalies in the last 15 minutes.")
        return
    count = len(anomalies)
    services = sorted({svc for svc, _, _ in anomalies})
    first_svc, _, first_msg = anomalies[0]
    first_line = first_msg.strip().replace("\n", " ")[:240]
    # Ship full unwrapped lines with a generous total budget rather than a
    # tight per-line cap, so one rich stack trace doesn't get chopped while
    # twenty short lines still fit.
    SAMPLE_TOTAL_BUDGET = 8000
    PER_LINE_HARD_CAP = 1500
    sample_parts: list[str] = []
    running = 0
    for svc, _, msg in anomalies[:20]:
        cleaned = msg.strip()
        if len(cleaned) > PER_LINE_HARD_CAP:
            cleaned = cleaned[:PER_LINE_HARD_CAP] + "…[truncated]"
        entry = f"[{svc}] {cleaned}"
        if running + len(entry) + 1 > SAMPLE_TOTAL_BUDGET:
            sample_parts.append(
                f"…[{len(anomalies[:20]) - len(sample_parts)} more line(s) omitted for budget]"
            )
            break
        sample_parts.append(entry)
        running += len(entry) + 1
    sample = "\n".join(sample_parts)
    detail = (
        f"NetSage alert at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}. "
        f"{count} anomalous log line(s) across {len(services)} service(s). "
        f"First line: {first_line}\n\n"
        f"Sample lines:\n{sample}"
    )
    broker_skippy(detail)
    print(f"OK: dispatched {count} anomalies to Skippy")


if __name__ == "__main__":
    main()
