#!/usr/bin/env python3
"""NetSage alert pass — Python-only to avoid codex shell-quoting hazards.

Runs once per scheduler fire. Pulls the last 15 minutes of logs from
Loki, picks out anomalous lines, and on a real finding dispatches a
detailed report to Hex via the broker as a self-message. Hex's
sentinel-triage pipeline owns classification, rule application, and
deciding whether anything warrants paging Daniel — most routine fires
are silenced by an approved rule without anyone being notified. Only
findings that survive triage escalate to Daniel, with Skippy as backup.
Prints a one-line status to stdout for the scheduler to echo back.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

LOKI_URL = "http://sentinel-loki:3100/loki/api/v1/query_range"
HEX_MIND_ID = "1d284b0a-0a45-40db-82a4-c4a984d0ee49"

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

LOKI_WINDOW_NS = 15 * 60 * 1_000_000_000
LOKI_LIMIT = 2000
HEARTBEAT_INTERVAL_SECONDS = 24 * 60 * 60
HEARTBEAT_STATE_PATH = Path(
    os.environ.get("NETSAGE_HEARTBEAT_STATE_PATH", str(Path(__file__).resolve().parents[3] / "data" / "netsage_heartbeat.json"))
)

# The feed is pulled with two queries, not one. Suricata floods Loki with
# sev-3 eve noise (Ethertype unknown, our own Telegram traffic, CLOSE_WAIT) at
# thousands of lines per 15-minute window — far over LOKI_LIMIT. A single
# `{service_name=~".+"}` pull therefore returns almost nothing but that noise,
# burying both the rare real Suricata alerts and genuine errors from every
# other service. So:
#   1. the generic query EXCLUDES Suricata, keeping the budget for everyone else;
#   2. Suricata is pulled on its own, pre-filtered to forward-worthy severities
#      at the Loki layer. Vector nests the eve.json as a JSON string inside the
#      envelope's `message` field, so severity appears in the escaped form
#      `\"severity\":N`; the regex below matches the two escape chars with `..`.
GENERIC_QUERY = '{service_name=~".+", source!="suricata"}'
SURICATA_ALERT_QUERY = r'{source="suricata", event_type="alert"} |~ `severity..:[12]`'


def _query_loki(logql: str) -> list[tuple[str, str, str, str]]:
    end_ns = time.time_ns()
    start_ns = end_ns - LOKI_WINDOW_NS
    query = urllib.parse.urlencode({
        "query": logql,
        "start": start_ns,
        "end": end_ns,
        "limit": LOKI_LIMIT,
        "direction": "backward",
    })
    try:
        with urllib.request.urlopen(f"{LOKI_URL}?{query}", timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"Loki query failed: {exc}")
        return []
    lines: list[tuple[str, str, str, str]] = []
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
        # Vector tags every Loki stream with its origin in the `source` label
        # (docker / journald / auth / suricata / zeek / clamav). The anomaly
        # picker gates on this, so carry it through rather than discard it.
        src = meta.get("source") or ""
        for entry in stream.get("values", []):
            ts, msg = entry[0], entry[1]
            lines.append((src, svc, ts, msg))
    return lines


def pull_loki_lines() -> list[tuple[str, str, str, str]]:
    return _query_loki(GENERIC_QUERY) + _query_loki(SURICATA_ALERT_QUERY)


def _decode_envelope(msg: str | None) -> dict | None:
    """Return the parsed dict if msg is a JSON log envelope, else None."""
    if not isinstance(msg, str):
        return None
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


def _eve_record(envelope: dict | None) -> dict | None:
    """Unwrap the real Suricata eve.json record from a Loki envelope.

    Vector tails eve.json (each line already JSON) and ships it as a *string*
    in the envelope's ``message`` field — it does NOT flatten the eve fields to
    the top level. So the event_type, alert{} sub-object, and src_ip/dest_ip
    that matter all live inside ``envelope["message"]``, not on ``envelope``
    itself. Parse that nested string; fall back to the envelope only if there
    is no message to unwrap (so a future flattened shape still works).
    """
    if not envelope:
        return None
    inner = _decode_envelope(envelope.get("message"))
    return inner if inner is not None else envelope


def _suricata_alert(record: dict | None) -> str | None:
    """Return a readable one-liner for a worth-forwarding Suricata alert.

    ``record`` is the unwrapped eve.json record (see :func:`_eve_record`): an
    alert event carries event_type=="alert" and an alert sub-object with
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
    src_port = record.get("src_port")
    dst = record.get("dest_ip")
    dst_port = record.get("dest_port")
    proto = record.get("proto")
    sev_label = severity if severity is not None else "?"
    parts = [f"Suricata alert severity {sev_label}: {signature}"]
    if category:
        parts.append(f"({category})")
    if src or dst:
        has_ports = src_port is not None or dst_port is not None
        src_label = f"{src or '?'}:{src_port or '?'}" if has_ports else f"{src or '?'}"
        dst_label = f"{dst or '?'}:{dst_port or '?'}" if has_ports else f"{dst or '?'}"
        parts.append(f"{src_label} to {dst_label}")
    if proto:
        parts.append(f"proto {str(proto).upper()}")
    return " ".join(parts)


def pick_anomalies(lines):
    out = []
    for src, svc, ts, msg in lines:
        record = _decode_envelope(msg)
        if src == "suricata":
            # Suricata is gated entirely on the Loki stream's `source` label,
            # never the generic text filter: its eve.json carries no source
            # marker of its own, and its stats counters embed crisis words
            # ("errors", "invalid") that would otherwise page on routine
            # telemetry. The real eve record is nested as a JSON string in
            # record["message"] — unwrap it before reading event_type/alert.
            eve = _eve_record(record)
            suricata_line = _suricata_alert(eve)
            if suricata_line is not None:
                host = (record.get("host") if record else None) or svc
                out.append((f"suricata@{host}", ts, suricata_line))
            # Everything else Suricata (stats/flow/dns/tls, sub-ceiling
            # alerts) is intentionally dropped here, never text-filtered.
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


def broker_hex(detail: str) -> None:
    broker_url = os.environ.get("HIVEMIND_BROKER_URL")
    broker_token = os.environ.get("HIVEMIND_BROKER_TOKEN")
    if not (broker_url and broker_token):
        print("Broker env missing; skipping Hex dispatch")
        return
    body = {
        "message_id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "from": HEX_MIND_ID,
        "to": HEX_MIND_ID,
        "content": detail,
        "rolling_summary": "",
        "metadata": {
            "request_type": "sentinel_alert",
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


def _heartbeat_due(now: float, state_path: Path | None = None) -> bool:
    state_path = HEARTBEAT_STATE_PATH if state_path is None else state_path
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError, TypeError):
        return True
    try:
        last_sent = float(state.get("last_sent", 0))
    except (TypeError, ValueError):
        return True
    return now - last_sent >= HEARTBEAT_INTERVAL_SECONDS


def _mark_heartbeat_sent(now: float, state_path: Path | None = None) -> None:
    state_path = HEARTBEAT_STATE_PATH if state_path is None else state_path
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "last_sent": now,
                    "last_sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                }
            )
        )
    except OSError as exc:
        print(f"Heartbeat state write failed: {exc}")


def maybe_send_heartbeat(now: float | None = None) -> bool:
    """Dispatch a once-a-day liveness ping to Hex when a scheduler pass finds

    nothing anomalous. Without this, "no anomalies" and "the whole NetSage
    pass silently died" look identical from Daniel's side — a quiet pass
    should be distinguishable from no pass at all.
    """
    now = time.time() if now is None else now
    if not _heartbeat_due(now):
        return False
    broker_hex(
        "Sentinel daily heartbeat at "
        f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}. "
        "NetSage scheduler pass is alive; no anomalous lines required for this heartbeat."
    )
    _mark_heartbeat_sent(now)
    return True


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
        heartbeat_sent = maybe_send_heartbeat()
        if args.json:
            print(json.dumps({"anomalies": [], "count": 0, "heartbeat_sent": heartbeat_sent}))
        else:
            suffix = " Daily heartbeat dispatched." if heartbeat_sent else ""
            print(f"No anomalies in the last 15 minutes.{suffix}")
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
        f"Sentinel alert at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}. "
        f"{count} anomalous log line(s) across {len(services)} service(s). "
        f"First line: {first_line}\n\n"
        f"Sample lines:\n{sample}"
    )
    broker_hex(detail)
    print(f"OK: dispatched {count} anomalies to Hex")


if __name__ == "__main__":
    main()
