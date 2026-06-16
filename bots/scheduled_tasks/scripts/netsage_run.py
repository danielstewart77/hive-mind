#!/usr/bin/env python3
"""NetSage alert pass — Python-only to avoid codex shell-quoting hazards.

Runs once per scheduler fire. Pulls the last 15 minutes of logs from
Loki, picks out anomalous lines, and on a real finding dispatches a
detailed report to Skippy via the broker as a self-message. Bilby is
no longer involved. Skippy's event-triage pipeline owns classification,
rule application, and notifying Daniel when something warrants his hands.
Prints a one-line status to stdout for the scheduler to echo back.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

LOKI_URL = "http://sentinel-loki:3100/loki/api/v1/query_range"
SKIPPY_MIND_ID = "14cb820b-4a42-4f04-a593-54f532fd1d2f"

ANOMALY_PATTERNS = ("error", "critical", "panic", "traceback", "denied", "fatal")
ANOMALY_REGEX = re.compile(
    r"\b(?:" + "|".join(ANOMALY_PATTERNS) + r")\b", re.IGNORECASE
)
MAX_JOURNAL_PRIORITY = 4

# Suricata IDS expresses badness through a numeric severity field and a
# signature name, not the English crisis words ANOMALY_REGEX hunts for, so
# eve.json alerts slip past the generic text filter entirely. Suricata
# severity is inverted: 1 is the most severe (active attack), 3 the least
# (informational noise such as "Ethertype unknown" or our own Telegram
# traffic showing up in ET HUNTING rules). Forward severity 1 and 2; drop 3.
SURICATA_SEVERITY_CEILING = 2
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


def _suricata_alert(record: dict | None) -> str | None:
    """Return a readable one-liner for a worth-forwarding Suricata alert.

    Vector merges the parsed eve.json line into the record, so an alert event
    carries a top-level event_type=="alert" and an alert sub-object with
    severity/signature/category. Returns None for non-alert Suricata events
    (flow, dns, tls, stats…) and for alerts at or below the noise ceiling.
    """
    if not record or record.get("event_type") != "alert":
        return None
    alert = record.get("alert")
    if not isinstance(alert, dict):
        return None
    try:
        severity = int(alert.get("severity"))
    except (TypeError, ValueError):
        severity = None
    if severity is not None and severity > SURICATA_SEVERITY_CEILING:
        return None
    signature = alert.get("signature") or "unknown signature"
    category = alert.get("category") or ""
    src = record.get("src_ip")
    dst = record.get("dest_ip")
    sev_label = severity if severity is not None else "?"
    parts = [f"Suricata alert severity {sev_label}: {signature}"]
    if category:
        parts.append(f"({category})")
    if src or dst:
        parts.append(f"{src or '?'} to {dst or '?'}")
    return " ".join(parts)


def pick_anomalies(lines):
    out = []
    for svc, ts, msg in lines:
        record = _decode_envelope(msg)
        suricata_line = _suricata_alert(record)
        if suricata_line is not None:
            host = record.get("host") if record else None
            out.append((f"suricata@{host or svc}", ts, suricata_line))
            continue
        if record and record.get("source_app") == "suricata":
            # Any other Suricata event (stats, flow, dns, tls…) only ever
            # surfaces via the alert path above. Skip it before the generic
            # text filter, whose crisis words (e.g. "error") match substrings
            # in Suricata's own internal stats counter names.
            continue
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
        "from": SKIPPY_MIND_ID,
        "to": SKIPPY_MIND_ID,
        "content": detail,
        "rolling_summary": "",
        "metadata": {
            "request_type": "netsage_alert",
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


def _build_sample(anomalies, total_budget=8000, per_line_cap=1500) -> str:
    parts: list[str] = []
    running = 0
    for svc, _, msg in anomalies[:20]:
        cleaned = msg.strip()
        if len(cleaned) > per_line_cap:
            cleaned = cleaned[:per_line_cap] + "…[truncated]"
        entry = f"[{svc}] {cleaned}"
        if running + len(entry) + 1 > total_budget:
            parts.append(
                f"…[{len(anomalies[:20]) - len(parts)} more line(s) omitted for budget]"
            )
            break
        parts.append(entry)
        running += len(entry) + 1
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit anomalies as JSON to stdout and skip broker dispatch.",
    )
    args = parser.parse_args(argv)

    lines = pull_loki_lines()
    anomalies = pick_anomalies(lines)
    if not anomalies:
        if args.json:
            print(json.dumps({"anomalies": [], "count": 0}))
        else:
            print("No anomalies in the last 15 minutes.")
        return

    count = len(anomalies)
    services = sorted({svc for svc, _, _ in anomalies})
    first_svc, _, first_msg = anomalies[0]
    first_line = first_msg.strip().replace("\n", " ")[:240]

    if args.json:
        payload = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": count,
            "services": services,
            "first_line": first_line,
            "anomalies": [
                {"service": svc, "ts": ts, "message": msg}
                for svc, ts, msg in anomalies[:20]
            ],
        }
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
        return

    sample = _build_sample(anomalies)
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
