#!/usr/bin/env python3
"""Deterministic processor for inbound NetSage / Sentinel broker dispatches.

Hex's session is spawned by hive-comms when a sensor alert is dispatched.
Instead of burning LLM tokens reasoning about every routine fire, the
session forwards the alert content to this tool, which:

  1. Classifies the content into a known event_class slug (or
     unclassified_anomaly as the catch-all).
  2. Writes an event row directly to events.db.
  3. Looks up the first approved auto-apply response_rule for that class.
     An approved rule always wins: record_only silences the event, no
     matter how many times the class has fired before or how long it has
     been recurring. Rewriting a class's disposition is a deliberate act
     (see sentinel-decide) — POST a new/narrower rule, don't let a
     background counter overrule it.
  4. Only when NO approved rule matches does the recurrence tracker run.
     It never notifies anyone — it computes how hard the class is
     recurring and returns an advisory `recurrence_target` ("skippy" /
     "daniel" / null) in the JSON so Hex's sentinel-triage skill knows
     the investigate + decide legs are mandatory this turn. The decision
     — silence with a rule, or page with a verdict — belongs to
     sentinel-decide, the only place a notification is ever sent.
  5. Executes the action (notify_daniel template substitution and a
     subprocess call into tools/stateless/notify/notify.py; record_only
     is a no-op; escalate_for_review pages via Telegram with the
     raw alert).
  6. Updates the event row's status and response_rule_id.

Prints a one-line JSON summary the session can forward as its broker reply.

The tool also carries Hex's pen: `process_event.py add-rule` writes an
approved auto-apply response_rule directly to the local events.db, so
sentinel-decide can silence a class deterministically without any network
dependency (the old flow pointed at an event-triage HTTP API that is not
reachable from the Sentinel host).

This is the merged alert-compose code from the Skippy repo
(hive_mind_skippy, tools/stateless/event_triage/process_event.py). The
explicit alert-line composer (compose_alert_fields / render_alert_line /
_alert_line_from_payload) is what the sentinel-decide skill invokes when it
escalates, so every Daniel-facing alert leads with the explicit one-line
story: source IP + port, destination IP + port, protocol, signature.

DB and .env paths are environment-overridable so the tool runs unchanged in
Hex's container, on the host, or in tests:

  EVENT_TRIAGE_DB_PATH   path to events.db (default: data/events.db at repo root)
  EVENT_TRIAGE_ENV_PATH  path to a .env to best-effort load (default: repo .env)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]

DB_PATH = Path(
    os.environ.get(
        "EVENT_TRIAGE_DB_PATH",
        str(_REPO_ROOT / "data" / "events.db"),
    )
)
NOTIFY_SCRIPT = Path(__file__).resolve().parents[1] / "notify" / "notify.py"
# Preferred first: process_event.py is invoked as a bare `python3 ...` by
# whatever shell the caller (Hex's codex exec_command, a cron line, a test
# runner) happens to be using, and that shell's PATH does not always resolve
# to the venv with httpx/anthropic/etc installed. Fall back to sys.executable
# (correct on the bare host and in tests) when the venv path does not exist.
SERVICE_PYTHON = Path("/opt/venv/bin/python3")
ENV_PATH = Path(os.environ.get("EVENT_TRIAGE_ENV_PATH", str(_REPO_ROOT / ".env")))
RECURRENCE_WINDOW = _dt.timedelta(minutes=15)
RECURRENCE_RATE_THRESHOLD = 10
RECURRENCE_PERSISTENCE_THRESHOLD = _dt.timedelta(minutes=15)
RECURRENCE_DANIEL_RATE_THRESHOLD = 50
_RECURRENCE_STATUSES = ("escalated_to_skippy", "escalated_to_daniel")


def _load_env() -> None:
    """Best-effort .env loader so the script works when invoked outside systemd."""
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)
    except OSError:
        pass


_load_env()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_COUNT_RE = re.compile(r"(\d+)\s+anomalous\s+log\s+line", re.IGNORECASE)

_KNOWN_BUCKETS = (
    "application_error",
    "infrastructure_noise",
    "security_signal",
    "third_party_noise",
    "performance_degradation",
    "network_traffic",
    "unclassified",
)


def _call_ollama_structured(prompt: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    url = os.environ.get("HIVE_TOOLS_URL")
    token = os.environ.get("HIVE_TOOLS_TOKEN")
    if not url or not token:
        return None
    body = json.dumps({"prompt": prompt, "schema": schema}).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/ollama/structured",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


def _llm_classify(
    content: str, catalog: list[dict[str, Any]]
) -> dict[str, Any] | None:
    schema = {
        "type": "object",
        "properties": {
            "chosen_slug": {"type": ["string", "null"]},
            "needs_new_class": {"type": "boolean"},
            "proposed_class": {
                "type": ["object", "null"],
                "properties": {
                    "slug": {"type": "string"},
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "bucket": {"type": "string"},
                },
                "required": ["slug", "label", "description", "bucket"],
                "additionalProperties": False,
            },
            "reasoning": {"type": "string"},
        },
        "required": ["chosen_slug", "needs_new_class", "reasoning"],
        "additionalProperties": False,
    }
    prompt = (
        "You are triaging a NetSage log anomaly alert from the sensor. Pick the best-fit "
        "event class from the catalog, or propose a new one if nothing fits.\n\n"
        "Be precise about which service is the actual subject of the log line. "
        "Vector wraps every container's logs in a source-metadata envelope, so "
        "the presence of that envelope is NOT the story — read the inner content "
        "to determine whether the underlying service (caddy, hive-comms, ollama, "
        "etc.) is actually erroring or whether the lines are pure infrastructure "
        "noise from vector itself.\n\n"
        "Distinguish between:\n"
        "  application_error: a service is actually failing or degraded\n"
        "  infrastructure_noise: log shippers / sidecars / metadata leaks with no impact\n"
        "  third_party_noise: external apps (vscode, browser extensions) with no impact on hive\n"
        "  security_signal: auth failures, intrusions, suspicious access, IDS alerts\n"
        "  performance_degradation: slow responses, timeouts, resource exhaustion\n"
        "  network_traffic: routine flow, DNS, TLS, mDNS, stats telemetry from passive capture\n"
        "  unclassified: catch-all\n\n"
        f"Buckets must be one of: {', '.join(_KNOWN_BUCKETS)}.\n\n"
        f"Catalog:\n{json.dumps(catalog, indent=2)}\n\n"
        "Alert content (truncated to 8000 chars):\n"
        f"---\n{content[:8000]}\n---\n\n"
        "Rules:\n"
        "  - If a catalog entry is a clean fit, set chosen_slug to its slug and "
        "needs_new_class=false. proposed_class must be null.\n"
        "  - If nothing fits, set chosen_slug=null, needs_new_class=true, and "
        "fill proposed_class with a kebab-case slug, short label, one-sentence "
        "description, and a bucket from the list above.\n"
        "  - Never invent a slug that is not in the catalog without setting needs_new_class=true.\n"
        "  - Caddy errors are application_error, not infrastructure_noise.\n"
    )
    return _call_ollama_structured(prompt, schema)


def _ensure_class(
    conn: sqlite3.Connection, slug: str, label: str, description: str, bucket: str
) -> sqlite3.Row:
    """Insert a new event_class if absent and return the row."""
    if bucket not in _KNOWN_BUCKETS:
        bucket = "unclassified"
    slug = re.sub(r"[^a-z0-9_]+", "_", slug.lower()).strip("_") or "unclassified_anomaly"
    conn.execute(
        """
        INSERT OR IGNORE INTO event_classes (slug, label, description, bucket)
        VALUES (?, ?, ?, ?)
        """,
        (slug, label[:120] or slug, description[:500] or "", bucket),
    )
    row = conn.execute(
        "SELECT * FROM event_classes WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"failed to insert event_class slug={slug!r} bucket={bucket!r} "
            "(check schema bucket constraint stayed in sync with _KNOWN_BUCKETS)"
        )
    return row


def classify(
    content: str, conn: sqlite3.Connection
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return (slug, extracted_payload, classify_meta).

    LLM is the only classifier. If it is unreachable we record the event
    as unclassified rather than guessing with a stale rule.
    """
    count_match = _COUNT_RE.search(content)
    count = int(count_match.group(1)) if count_match else None
    payload: dict[str, Any] = {"count": count, "excerpt": content[:4000]}
    eve = _extract_embedded_eve(content)
    if eve is not None:
        payload["eve"] = eve

    catalog = [
        {
            "slug": r["slug"],
            "label": r["label"],
            "description": r["description"],
            "bucket": r["bucket"],
        }
        for r in conn.execute(
            "SELECT slug, label, description, bucket FROM event_classes ORDER BY id"
        )
    ]
    known_slugs = {c["slug"] for c in catalog}

    llm_meta: dict[str, Any] = {"path": "llm"}
    llm = _llm_classify(content, catalog)
    if llm is None:
        llm_meta["path"] = "classifier_offline"
        llm_meta["reasoning"] = "ollama unreachable; recording as unclassified"
        return "unclassified_anomaly", payload, llm_meta

    llm_meta["reasoning"] = llm.get("reasoning", "")
    chosen = llm.get("chosen_slug")
    needs_new = bool(llm.get("needs_new_class"))
    proposed = llm.get("proposed_class") or {}

    if needs_new and proposed.get("slug"):
        row = _ensure_class(
            conn,
            slug=proposed["slug"],
            label=proposed.get("label", proposed["slug"]),
            description=proposed.get("description", ""),
            bucket=proposed.get("bucket", "unclassified"),
        )
        llm_meta["proposed_class_inserted"] = row["slug"]
        return row["slug"], payload, llm_meta

    if isinstance(chosen, str) and chosen in known_slugs:
        return chosen, payload, llm_meta

    # LLM returned something unusable — fall back to catch-all rather than
    # mis-routing to a silent rule.
    llm_meta["path"] = "llm_unusable"
    return "unclassified_anomaly", payload, llm_meta


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_events_status_schema(conn)
    return conn


def _ensure_events_status_schema(conn: sqlite3.Connection) -> None:
    """Allow recurrence escalation statuses in older local event stores."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return
    table_sql = row["sql"]
    if all(status in table_sql for status in _RECURRENCE_STATUSES):
        return
    if "CHECK(status IN" not in table_sql:
        return

    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        ALTER TABLE events RENAME TO events_old_status_check;
        CREATE TABLE events (
            id                INTEGER PRIMARY KEY,
            event_class_id    INTEGER NOT NULL REFERENCES event_classes(id),
            source            TEXT NOT NULL,
            occurred_at       TEXT NOT NULL,
            payload_json      TEXT NOT NULL DEFAULT '{}',
            summary           TEXT,
            severity          TEXT CHECK(severity IN ('info','low','medium','high','critical')),
            status            TEXT NOT NULL DEFAULT 'awaiting_triage' CHECK(status IN (
                                  'awaiting_triage',
                                  'auto_acted',
                                  'notified_daniel',
                                  'escalated',
                                  'ignored',
                                  'awaiting_decision',
                                  'escalated_to_skippy',
                                  'escalated_to_daniel'
                              )),
            response_rule_id  INTEGER REFERENCES response_rules(id),
            action_log        TEXT,
            created_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO events (
            id,
            event_class_id,
            source,
            occurred_at,
            payload_json,
            summary,
            severity,
            status,
            response_rule_id,
            action_log,
            created_at
        )
        SELECT
            id,
            event_class_id,
            source,
            occurred_at,
            payload_json,
            summary,
            severity,
            status,
            response_rule_id,
            action_log,
            created_at
        FROM events_old_status_check;
        DROP TABLE events_old_status_check;
        PRAGMA foreign_keys = ON;
        """
    )


def _get_class_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM event_classes WHERE slug = ?", (slug,)
    ).fetchone()


def _insert_event(
    conn: sqlite3.Connection,
    event_class_id: int,
    source: str,
    occurred_at: str,
    payload: dict[str, Any],
    summary: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO events (event_class_id, source, occurred_at, payload_json, summary, status)
        VALUES (?, ?, ?, ?, ?, 'awaiting_triage')
        """,
        (event_class_id, source, occurred_at, json.dumps(payload), summary),
    )
    if cur.lastrowid is None:
        raise RuntimeError("event insert did not return a row id")
    return int(cur.lastrowid)


# Matches "<src-ip> to <dst-ip>" as emitted by Suricata/NetSage alert lines.
_SRC_DST_RE = re.compile(
    r"(\d{1,3}(?:\.\d{1,3}){3})\s+to\s+(\d{1,3}(?:\.\d{1,3}){3})"
)


def _extract_embedded_eve(content: str) -> dict[str, Any] | None:
    """Find a raw Suricata eve object embedded as a JSON line in alert text."""
    for line in content.splitlines():
        text = line.strip()
        if text.startswith("EVE:"):
            text = text[4:].strip()
        if not text.startswith("{"):
            continue
        try:
            record = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("src_ip") and record.get("dest_ip") and isinstance(
            record.get("alert"), dict
        ):
            return record
    return None


def _extract_source_ip(payload: dict[str, Any]) -> str | None:
    """Pull the source IPv4 out of an alert payload's excerpt/summary.

    NetSage Suricata lines read "<src> to <dst>"; the source is the first
    capture. Returns None when no such pair is present.
    """
    for field in ("excerpt", "summary"):
        text = payload.get(field)
        if not isinstance(text, str):
            continue
        m = _SRC_DST_RE.search(text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Explicit alert composition (backlog "Alert format — the last-leg fix")
# ---------------------------------------------------------------------------
#
# Every Daniel-facing alert must lead with a single explicit line that states
# the source IP + port, destination IP + port, and protocol — never a vague
# "RDP alert" with no pivot facts. The composer pulls those six fields out of
# a raw Suricata eve record; the renderer turns them into one line. Missing
# fields render as "?" rather than being dropped, so the one-line story is
# never silently incomplete.


def compose_alert_fields(eve: dict[str, Any]) -> dict[str, Any]:
    """Extract the explicit alert fields from a raw Suricata eve record.

    Reads the canonical eve keys (``src_ip``, ``src_port``, ``dest_ip``,
    ``dest_port``, ``proto``, ``alert.signature``). Returns a dict with the
    keys ``src_ip``, ``src_port``, ``dst_ip``, ``dst_port``, ``proto``,
    ``signature``; any field absent from the record is ``None``. Ports are
    coerced to ``int`` when present and parseable, else left ``None``.
    """

    def _port(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    raw_alert = eve.get("alert")
    alert: dict[str, Any] = raw_alert if isinstance(raw_alert, dict) else {}
    proto = _str(eve.get("proto"))
    return {
        "src_ip": _str(eve.get("src_ip")),
        "src_port": _port(eve.get("src_port")),
        "dst_ip": _str(eve.get("dest_ip")),
        "dst_port": _port(eve.get("dest_port")),
        "proto": proto.upper() if proto else None,
        "signature": _str(alert.get("signature")),
    }


def render_alert_line(fields: dict[str, Any]) -> str:
    """Render the explicit one-line alert story from composed fields.

    Shape: ``<src_ip>:<src_port> → <dst_ip>:<dst_port> proto <PROTO> — <signature>``.
    Missing fields are rendered as ``?`` so a field is never silently dropped.
    The signature suffix is omitted only when there is no signature.
    """

    def _show(value: Any) -> str:
        if value is None or value == "":
            return "?"
        return str(value)

    src = f"{_show(fields.get('src_ip'))}:{_show(fields.get('src_port'))}"
    dst = f"{_show(fields.get('dst_ip'))}:{_show(fields.get('dst_port'))}"
    line = f"{src} → {dst} proto {_show(fields.get('proto'))}"
    signature = fields.get("signature")
    if signature:
        line += f" — {signature}"
    return line


def _alert_line_from_payload(payload: dict[str, Any]) -> str | None:
    """Return the explicit alert line for a payload that carries an eve record.

    The eve record may ride on the payload under ``eve`` or ``eve_record``;
    when present, compose + render it. Returns ``None`` when no eve record is
    attached so non-eve alerts are unaffected.
    """
    for key in ("eve", "eve_record"):
        eve = payload.get(key)
        if isinstance(eve, dict):
            return render_alert_line(compose_alert_fields(eve))
    return None


def _condition_matches(condition_expr: str | None, payload: dict[str, Any]) -> bool:
    """Evaluate a rule's condition against the event payload.

    Grammar is deliberately tiny so rules stay data, not code:
      - None / "" / "always"  -> always matches (every legacy rule)
      - "src_in:<cidr>[,<cidr>...]" -> matches when the alert's source IP
        falls inside one of the listed CIDRs
      - "excerpt_contains:<substr>" -> matches when the alert's excerpt or
        summary contains the literal substring (case-insensitive). The
        remainder is one literal needle, not a comma list, so log phrases
        with commas work verbatim.
    Any unrecognised syntax fails closed (no match) so a malformed rule can
    never silently auto-apply.
    """
    if condition_expr is None:
        return True
    expr = condition_expr.strip()
    if expr in ("", "always"):
        return True
    if expr.startswith("src_in:"):
        src = _extract_source_ip(payload)
        if src is None:
            return False
        try:
            addr = ipaddress.ip_address(src)
        except ValueError:
            return False
        for chunk in expr[len("src_in:"):].split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                if addr in ipaddress.ip_network(chunk, strict=False):
                    return True
            except ValueError:
                continue
        return False
    if expr.startswith("excerpt_contains:"):
        needle = expr[len("excerpt_contains:"):].strip().lower()
        if not needle:
            return False
        for field in ("excerpt", "summary"):
            hay = payload.get(field)
            if isinstance(hay, str) and needle in hay.lower():
                return True
        return False
    return False


def _find_rule(
    conn: sqlite3.Connection, event_class_id: int, payload: dict[str, Any]
) -> sqlite3.Row | None:
    """First approved, auto-apply rule for the class whose condition matches.

    Rules are tried in id order; the first whose ``condition_expr`` matches
    the payload wins. When none match, the caller falls through to
    awaiting_decision (or the recurrence gate) — exactly what should happen
    for an out-of-scope hit.
    """
    rows = conn.execute(
        """
        SELECT * FROM response_rules
         WHERE event_class_id = ?
           AND approval_state = 'approved'
           AND auto_apply = 1
         ORDER BY id
        """,
        (event_class_id,),
    ).fetchall()
    for row in rows:
        if _condition_matches(row["condition_expr"], payload):
            return row
    return None


def _mark_event(
    conn: sqlite3.Connection,
    event_id: int,
    status: str,
    rule_id: int | None,
    action_log: str | None,
) -> None:
    conn.execute(
        "UPDATE events SET status = ?, response_rule_id = ?, action_log = ? WHERE id = ?",
        (status, rule_id, action_log, event_id),
    )


def _touch_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute(
        "UPDATE response_rules SET last_fired_at = datetime('now'), fire_count = fire_count + 1 WHERE id = ?",
        (rule_id,),
    )


# ---------------------------------------------------------------------------
# Recurrence tracker (advisory only)
# ---------------------------------------------------------------------------
#
# For a class with NO approved auto-apply rule, an event can otherwise sit in
# awaiting_decision forever without anyone ever noticing it needs a decision.
# The tracker computes how hard such a class is recurring and surfaces an
# advisory target ("skippy" / "daniel" / None) in the result JSON. It NEVER
# sends a notification itself: paging from inside this deterministic tool is
# what turned Hex into a dumb relay — the alert reached Daniel before Hex's
# investigate/decide legs ever ran, and triage read the escalated status as
# "already handled". The advisory instead makes those legs mandatory for the
# session (see sentinel-triage), and sentinel-decide remains the only place a
# notification is ever sent. It also never runs for a class that already has
# an approved rule — see process() below, which only consults it when
# _find_rule returned None.


def _parse_utc(value: str) -> _dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return _dt.datetime.now(_dt.UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_dt.UTC)
    return parsed.astimezone(_dt.UTC)


def _recurrence_meta(
    conn: sqlite3.Connection,
    event_class_id: int,
    occurred_at: str,
) -> dict[str, Any]:
    current = _parse_utc(occurred_at)
    window_start = current - RECURRENCE_WINDOW
    rows = conn.execute(
        """
        SELECT id, occurred_at, status
          FROM events
         WHERE event_class_id = ?
         ORDER BY occurred_at, id
        """,
        (event_class_id,),
    ).fetchall()
    total_count = len(rows)
    window_rows = [r for r in rows if _parse_utc(r["occurred_at"]) >= window_start]
    first_seen = _parse_utc(rows[0]["occurred_at"]) if rows else current
    last_seen = _parse_utc(rows[-1]["occurred_at"]) if rows else current
    persisted_for = max(_dt.timedelta(0), current - first_seen)
    previous_skippy = any(r["status"] == "escalated_to_skippy" for r in rows[:-1])
    previous_daniel = any(r["status"] == "escalated_to_daniel" for r in rows[:-1])
    return {
        "total_count": total_count,
        "window_count": len(window_rows),
        "first_seen": first_seen.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_seen": last_seen.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_minutes": int(RECURRENCE_WINDOW.total_seconds() // 60),
        "persisted_minutes": int(persisted_for.total_seconds() // 60),
        "previous_skippy_escalation": previous_skippy,
        "previous_daniel_escalation": previous_daniel,
    }


def _recurrence_target(meta: dict[str, Any]) -> str | None:
    window_count = int(meta["window_count"])
    persisted = _dt.timedelta(minutes=int(meta["persisted_minutes"]))
    if (
        window_count >= RECURRENCE_DANIEL_RATE_THRESHOLD
        or (
            meta["previous_skippy_escalation"]
            and (
                window_count >= RECURRENCE_RATE_THRESHOLD
                or persisted >= RECURRENCE_PERSISTENCE_THRESHOLD
            )
        )
    ):
        return "daniel"
    if (
        window_count >= RECURRENCE_RATE_THRESHOLD
        or persisted >= RECURRENCE_PERSISTENCE_THRESHOLD
    ):
        return "skippy"
    return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _send_notify(message: str, channels: list[str]) -> tuple[bool, str]:
    if not NOTIFY_SCRIPT.exists():
        return False, f"notify.py not found at {NOTIFY_SCRIPT}"
    python_executable = str(SERVICE_PYTHON if SERVICE_PYTHON.exists() else sys.executable)
    try:
        result = subprocess.run(
            [
                python_executable,
                str(NOTIFY_SCRIPT),
                "send",
                "--message",
                message,
                "--channels",
                ",".join(channels),
            ],
            timeout=30,
            capture_output=True,
            text=True,
        )
        ok = result.returncode == 0
        return ok, (result.stdout.strip() or result.stderr.strip())[:400]
    except Exception as exc:  # pragma: no cover
        return False, f"notify subprocess error: {exc}"


def execute_action(
    rule: sqlite3.Row, payload: dict[str, Any], class_row: sqlite3.Row, event_id: int
) -> tuple[str, str]:
    """Returns (new_status, action_log)."""
    kind = rule["action_kind"]
    params = json.loads(rule["action_params_json"] or "{}")

    if kind == "record_only":
        return "ignored", f"record_only rule={rule['id']}"

    if kind == "notify_daniel":
        template = params.get("message_template", "")
        channels = params.get("channels") or ["telegram"]
        try:
            message = template.format(**payload)
        except (KeyError, IndexError, ValueError):
            message = template + f"  (payload: {json.dumps(payload)[:300]})"
        alert_line = _alert_line_from_payload(payload)
        if alert_line:
            message = f"{alert_line}\n{message}"
        ok, detail = _send_notify(message, channels)
        status = "notified_daniel" if ok else "escalated"
        return status, f"notify_daniel ok={ok} detail={detail}"

    if kind == "escalate_for_review":
        # Surface to Hex / Daniel as an explicit review request.
        msg = (
            f"Event_triage rule {rule['id']} requests review for class "
            f"{class_row['slug']} (event #{event_id}). Excerpt: "
            f"{payload.get('excerpt','')[:240]}"
        )
        alert_line = _alert_line_from_payload(payload)
        if alert_line:
            msg = f"{alert_line}\n{msg}"
        ok, detail = _send_notify(msg, params.get("channels") or ["telegram"])
        return "escalated", f"escalate_for_review ok={ok} detail={detail}"

    if kind == "bounce_container":
        # Not authorized today; refuse and surface.
        msg = (
            f"Rule {rule['id']} wants to bounce container "
            f"{params.get('container','?')} but no execution path is authorized. "
            f"Manual restart required. Event #{event_id}, class {class_row['slug']}."
        )
        ok, detail = _send_notify(msg, ["telegram"])
        return "escalated", f"bounce_container refused; notified ok={ok} detail={detail}"

    # custom or unknown
    msg = (
        f"Unhandled action_kind={kind} for rule {rule['id']} on event "
        f"#{event_id} (class {class_row['slug']}). Manual handling required."
    )
    ok, _ = _send_notify(msg, ["telegram"])
    return "escalated", f"unhandled action_kind={kind} notified ok={ok}"


def escalate_no_rule(
    class_row: sqlite3.Row, payload: dict[str, Any], event_id: int
) -> tuple[str, str]:
    # No Daniel-facing notification here. The caller (Hex's session) is the
    # one that decides whether this case warrants paging Daniel; this function
    # just records that the event is waiting on a Hex decision and returns.
    return "awaiting_decision", f"no_rule for class={class_row['slug']} event={event_id}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def process(
    content: str,
    source: str = "bilby_netsage",
    occurred_at: str | None = None,
    class_slug: str | None = None,
    classifier_reasoning: str | None = None,
) -> dict[str, Any]:
    if occurred_at is None:
        occurred_at = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = (content.strip().splitlines() or [""])[0][:240]

    conn = _connect()
    try:
        if class_slug:
            count_match = _COUNT_RE.search(content)
            count = int(count_match.group(1)) if count_match else None
            payload: dict[str, Any] = {"count": count, "excerpt": content[:4000]}
            eve = _extract_embedded_eve(content)
            if eve is not None:
                payload["eve"] = eve
            classify_meta = {
                "path": "caller_provided",
                "reasoning": classifier_reasoning or "",
            }
            existing = _get_class_by_slug(conn, class_slug)
            if existing is None:
                return {
                    "ok": False,
                    "error": f"caller-supplied class slug '{class_slug}' is not registered",
                }
            slug = class_slug
        else:
            slug, payload, classify_meta = classify(content, conn)
        payload["classify_meta"] = classify_meta
        class_row = _get_class_by_slug(conn, slug)
        if class_row is None:
            return {
                "ok": False,
                "error": f"unknown class slug '{slug}'; seed it in event_classes first",
            }
        event_id = _insert_event(
            conn,
            event_class_id=int(class_row["id"]),
            source=source,
            occurred_at=occurred_at,
            payload=payload,
            summary=summary,
        )
        recurrence = _recurrence_meta(conn, int(class_row["id"]), occurred_at)
        payload["recurrence"] = recurrence
        conn.execute(
            "UPDATE events SET payload_json = ? WHERE id = ?",
            (json.dumps(payload), event_id),
        )

        # An approved auto-apply rule always wins. The recurrence tracker is
        # advisory data for classes nobody has made a call on yet — it never
        # notifies (see the tracker comment block above _parse_utc).
        rule = _find_rule(conn, int(class_row["id"]), payload)
        recurrence_target: str | None = None
        if rule is not None:
            status, log = execute_action(rule, payload, class_row, event_id)
            _mark_event(conn, event_id, status, int(rule["id"]), log)
            _touch_rule(conn, int(rule["id"]))
            rule_id = int(rule["id"])
        else:
            recurrence_target = _recurrence_target(recurrence)
            status, log = escalate_no_rule(class_row, payload, event_id)
            if recurrence_target is not None:
                log += f" recurrence_target={recurrence_target}"
            _mark_event(conn, event_id, status, None, log)
            rule_id = None
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "event_id": event_id,
        "class_slug": slug,
        "class_bucket": class_row["bucket"],
        "rule_id": rule_id,
        "status": status,
        "action_log": log,
        "recurrence_target": recurrence_target,
        "payload": payload,
        "classify_meta": classify_meta,
    }


# ---------------------------------------------------------------------------
# add-rule — Hex's pen
# ---------------------------------------------------------------------------
#
# sentinel-decide silences a class by writing an approved auto-apply rule.
# The old skill text POSTed to the event-triage HTTP API, which is not
# reachable from the Sentinel host — so Hex could decide but never act.
# This subcommand writes the rule directly to the local events.db instead.

_VALID_ACTION_KINDS = ("record_only", "notify_daniel", "escalate_for_review")


def _condition_is_valid(condition_expr: str | None) -> bool:
    """Mirror _condition_matches's grammar so a rule that can never match is
    rejected at write time instead of failing closed forever at run time."""
    if condition_expr is None:
        return True
    expr = condition_expr.strip()
    if expr in ("", "always"):
        return True
    if expr.startswith("src_in:"):
        chunks = [c.strip() for c in expr[len("src_in:"):].split(",") if c.strip()]
        if not chunks:
            return False
        for chunk in chunks:
            try:
                ipaddress.ip_network(chunk, strict=False)
            except ValueError:
                return False
        return True
    if expr.startswith("excerpt_contains:"):
        return bool(expr[len("excerpt_contains:"):].strip())
    return False


def add_rule(
    class_slug: str,
    name: str,
    action_kind: str = "record_only",
    condition_expr: str | None = None,
    action_params: dict[str, Any] | None = None,
    authorized_by: str = "hex",
) -> dict[str, Any]:
    if action_kind not in _VALID_ACTION_KINDS:
        return {"ok": False, "error": f"action_kind must be one of {_VALID_ACTION_KINDS}"}
    if not _condition_is_valid(condition_expr):
        return {
            "ok": False,
            "error": "condition_expr must be empty/'always', 'src_in:<cidr>[,...]', "
            "or 'excerpt_contains:<substr>' — anything else fails closed at match time",
        }
    conn = _connect()
    try:
        class_row = _get_class_by_slug(conn, class_slug)
        if class_row is None:
            return {"ok": False, "error": f"unknown class slug '{class_slug}'"}
        existing = conn.execute(
            """
            SELECT id FROM response_rules
             WHERE event_class_id = ? AND action_kind = ?
               AND COALESCE(condition_expr, '') = COALESCE(?, '')
               AND approval_state = 'approved' AND auto_apply = 1
            """,
            (int(class_row["id"]), action_kind, condition_expr),
        ).fetchone()
        if existing is not None:
            return {
                "ok": True,
                "rule_id": int(existing["id"]),
                "created": False,
                "detail": "identical approved rule already exists",
            }
        # Older local stores lack the name/authorized_by columns the served
        # API's schema has; include them only where they exist.
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(response_rules)")
        }
        params_col = "action_params_json" if "action_params_json" in cols else "action_params"
        fields = ["event_class_id", "approval_state", "auto_apply", "condition_expr", "action_kind", params_col]
        values: list[Any] = [
            int(class_row["id"]),
            "approved",
            1,
            condition_expr,
            action_kind,
            json.dumps(action_params or {}),
        ]
        if "name" in cols:
            fields.append("name")
            values.append(name[:120])
        if "authorized_by" in cols:
            fields.append("authorized_by")
            values.append(authorized_by)
        placeholders = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO response_rules ({', '.join(fields)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        return {
            "ok": True,
            "rule_id": int(cur.lastrowid),
            "created": True,
            "class_slug": class_slug,
            "action_kind": action_kind,
            "condition_expr": condition_expr,
        }
    finally:
        conn.close()


def _add_rule_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="process_event.py add-rule",
        description="Write an approved auto-apply response rule to the local events.db",
    )
    p.add_argument("--class-slug", required=True)
    p.add_argument("--name", required=True, help="Short human-readable reason for the rule.")
    p.add_argument("--action-kind", default="record_only", choices=_VALID_ACTION_KINDS)
    p.add_argument(
        "--condition",
        default=None,
        help="Optional scope: 'src_in:<cidr>[,...]' or 'excerpt_contains:<substr>'. "
        "Omit for an unconditional rule.",
    )
    p.add_argument("--params-json", default="{}", help="action_params as a JSON object.")
    p.add_argument("--authorized-by", default="hex")
    args = p.parse_args(argv)
    try:
        params = json.loads(args.params_json)
    except ValueError:
        print(json.dumps({"ok": False, "error": "--params-json is not valid JSON"}))
        return 1
    result = add_rule(
        class_slug=args.class_slug,
        name=args.name,
        action_kind=args.action_kind,
        condition_expr=args.condition,
        action_params=params,
        authorized_by=args.authorized_by,
    )
    print(json.dumps(result, default=str))
    return 0 if result.get("ok") else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Process an inbound NetSage dispatch")
    p.add_argument("--content", help="Alert content. Reads stdin if omitted.")
    p.add_argument("--source", default="bilby_netsage")
    p.add_argument("--occurred-at", default=None, help="ISO timestamp; defaults to now.")
    p.add_argument(
        "--class-slug",
        default=None,
        help="If supplied, skip the LLM classifier and record under this slug. "
        "Used when the upstream caller (e.g. Bilby) has already classified.",
    )
    p.add_argument(
        "--classifier-reasoning",
        default=None,
        help="Optional reasoning string from the upstream classifier; "
        "stored in classify_meta.reasoning for audit.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["add-rule"]:
        return _add_rule_main(argv[1:])
    args = _parse_args(argv)
    content = args.content if args.content is not None else sys.stdin.read()
    if not content.strip():
        print(json.dumps({"ok": False, "error": "empty content"}))
        return 1
    result = process(
        content,
        source=args.source,
        occurred_at=args.occurred_at,
        class_slug=args.class_slug,
        classifier_reasoning=args.classifier_reasoning,
    )
    print(json.dumps(result, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
