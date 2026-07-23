"""Guards for event_triage's rule-application and recurrence-advisory behavior.

An approved auto-apply response_rule is a deliberate decision that a class
of alert is routine, and it always wins — locked in after a live incident
where a recurrence gate re-escalated already-silenced, weeks-old,
approved-noise classes on every recurrence.

The recurrence tracker itself is ADVISORY ONLY: for a class with no approved
rule it returns a recurrence_target ("skippy"/"daniel"/None) in the result so
Hex's sentinel-triage knows investigate+decide are mandatory, but it never
sends a notification — paging from inside the deterministic tool bypassed
Hex's decision legs entirely (the "dumb claxon" incident). sentinel-decide is
the only notifier, and it acts via the add-rule subcommand, also guarded here.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = _PROJECT_ROOT / "tools/stateless/event_triage/process_event.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("process_event", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


process_event = _load_module()


_SCHEMA = """
CREATE TABLE event_classes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  bucket TEXT NOT NULL
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_class_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  summary TEXT,
  status TEXT NOT NULL,
  response_rule_id INTEGER,
  action_log TEXT,
  FOREIGN KEY(event_class_id) REFERENCES event_classes(id)
);
CREATE TABLE response_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_class_id INTEGER NOT NULL,
  approval_state TEXT NOT NULL DEFAULT 'pending',
  auto_apply INTEGER NOT NULL DEFAULT 0,
  condition_expr TEXT,
  action_kind TEXT NOT NULL,
  action_params_json TEXT NOT NULL DEFAULT '{}',
  last_fired_at TEXT,
  fire_count INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(event_class_id) REFERENCES event_classes(id)
);
-- class 1: has an approved, auto-apply record_only rule.
INSERT INTO event_classes (slug, label, description, bucket)
VALUES ('nvidia_probe_failure', 'NVIDIA probe failure', 'Known GT218 driver probe failure', 'infrastructure_noise');
INSERT INTO response_rules (event_class_id, approval_state, auto_apply, condition_expr, action_kind)
VALUES (1, 'approved', 1, 'always', 'record_only');
-- class 2: no response_rules row at all -- nobody has decided on this one yet.
INSERT INTO event_classes (slug, label, description, bucket)
VALUES ('unclassified_repeat_noise', 'Unclassified repeat noise', 'No rule written yet', 'unclassified');
"""

_CHECKED_SCHEMA = """
CREATE TABLE event_classes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  bucket TEXT NOT NULL
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  event_class_id INTEGER NOT NULL REFERENCES event_classes(id),
  source TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  summary TEXT,
  severity TEXT CHECK(severity IN ('info','low','medium','high','critical')),
  status TEXT NOT NULL DEFAULT 'awaiting_triage' CHECK(status IN (
    'awaiting_triage',
    'auto_acted',
    'notified_daniel',
    'escalated',
    'ignored',
    'awaiting_decision'
  )),
  response_rule_id INTEGER REFERENCES response_rules(id),
  action_log TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE response_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_class_id INTEGER NOT NULL,
  approval_state TEXT NOT NULL DEFAULT 'pending',
  auto_apply INTEGER NOT NULL DEFAULT 0,
  condition_expr TEXT,
  action_kind TEXT NOT NULL,
  action_params_json TEXT NOT NULL DEFAULT '{}',
  last_fired_at TEXT,
  fire_count INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(event_class_id) REFERENCES event_classes(id)
);
INSERT INTO event_classes (slug, label, description, bucket)
VALUES ('nvidia_probe_failure', 'NVIDIA probe failure', 'Known GT218 driver probe failure', 'infrastructure_noise');
INSERT INTO response_rules (event_class_id, approval_state, auto_apply, condition_expr, action_kind)
VALUES (1, 'approved', 1, 'always', 'record_only');
INSERT INTO event_classes (slug, label, description, bucket)
VALUES ('unclassified_repeat_noise', 'Unclassified repeat noise', 'No rule written yet', 'unclassified');
"""


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def _init_checked_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_CHECKED_SCHEMA)
    conn.commit()
    conn.close()


def _patch_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(
        process_event,
        "_send_notify",
        lambda message, channels: sent.append((message, channels)) or (True, "sent"),
    )
    return sent


# ---------------------------------------------------------------------------
# An approved rule always wins, no matter how much a class recurs.
# ---------------------------------------------------------------------------


def test_known_benign_event_stays_ignored_on_first_fire(tmp_path, monkeypatch):
    db = tmp_path / "events.db"
    _init_db(db)
    monkeypatch.setattr(process_event, "DB_PATH", db)
    sent = _patch_notify(monkeypatch)

    result = process_event.process(
        "Sentinel alert at 2026-07-12T12:00:00Z. 1 anomalous log line.",
        occurred_at="2026-07-12T12:00:00Z",
        class_slug="nvidia_probe_failure",
    )

    assert result["status"] == "ignored"
    assert result["rule_id"] == 1
    assert result["payload"]["recurrence"]["window_count"] == 1
    assert sent == []


def test_approved_rule_class_stays_ignored_despite_heavy_recurrence(tmp_path, monkeypatch):
    """Regression guard: the recurrence gate must never override an approved rule.

    Fires an approved-rule class well past every recurrence threshold (rate,
    persistence, and the higher Daniel-rate threshold) and asserts it is
    silenced on every single occurrence -- this is exactly the live incident
    where months-old, already-approved noise classes kept re-paging Daniel.
    """
    db = tmp_path / "events.db"
    _init_db(db)
    monkeypatch.setattr(process_event, "DB_PATH", db)
    sent = _patch_notify(monkeypatch)

    for minute in range(60):
        result = process_event.process(
            f"Sentinel alert at 2026-07-12T12:{minute:02d}:00Z. 1 anomalous log line.",
            occurred_at=f"2026-07-12T12:{minute:02d}:00Z",
            class_slug="nvidia_probe_failure",
        )
        assert result["status"] == "ignored"
        assert result["rule_id"] == 1

    # Same class, a day later -- persisted_minutes is enormous; still silenced.
    result = process_event.process(
        "Sentinel alert at 2026-07-13T12:00:00Z. 1 anomalous log line.",
        occurred_at="2026-07-13T12:00:00Z",
        class_slug="nvidia_probe_failure",
    )
    assert result["status"] == "ignored"
    assert result["rule_id"] == 1
    assert sent == []


# ---------------------------------------------------------------------------
# The recurrence tracker is advisory only: it never notifies, it just tells
# the caller (Hex's session) that investigate+decide are mandatory this turn.
# ---------------------------------------------------------------------------


def test_rate_recurrence_advises_skippy_without_notifying(tmp_path, monkeypatch):
    db = tmp_path / "events.db"
    _init_db(db)
    monkeypatch.setattr(process_event, "DB_PATH", db)
    sent = _patch_notify(monkeypatch)

    for minute in range(9):
        result = process_event.process(
            f"Sentinel alert at 2026-07-12T12:{minute:02d}:00Z. 1 anomalous log line.",
            occurred_at=f"2026-07-12T12:{minute:02d}:00Z",
            class_slug="unclassified_repeat_noise",
        )
        assert result["status"] == "awaiting_decision"
        assert result["recurrence_target"] is None

    result = process_event.process(
        "Sentinel alert at 2026-07-12T12:09:30Z. 1 anomalous log line.",
        occurred_at="2026-07-12T12:09:30Z",
        class_slug="unclassified_repeat_noise",
    )

    assert result["status"] == "awaiting_decision"
    assert result["rule_id"] is None
    assert result["recurrence_target"] == "skippy"
    assert result["payload"]["recurrence"]["window_count"] == 10
    assert sent == []


def test_recurrence_statuses_are_allowed_in_checked_event_store(tmp_path, monkeypatch):
    """Hex writes escalated_to_skippy/escalated_to_daniel directly to events.db
    after sentinel-decide acts on a recurrence advisory (see the ladder test
    below); older local stores predate those statuses in their CHECK
    constraint, so _connect() must migrate the schema to allow them."""
    db = tmp_path / "events.db"
    _init_checked_db(db)
    monkeypatch.setattr(process_event, "DB_PATH", db)
    _patch_notify(monkeypatch)

    result = process_event.process(
        "Sentinel alert at 2026-07-12T12:00:00Z. 1 anomalous log line.",
        occurred_at="2026-07-12T12:00:00Z",
        class_slug="unclassified_repeat_noise",
    )
    assert result["status"] == "awaiting_decision"

    conn = process_event._connect()
    conn.execute(
        "UPDATE events SET status = 'escalated_to_skippy' WHERE id = ?",
        (result["event_id"],),
    )
    conn.commit()
    stored = conn.execute(
        "SELECT status FROM events WHERE id = ?", (result["event_id"],)
    ).fetchone()
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()[0]
    conn.close()
    assert stored["status"] == "escalated_to_skippy"
    assert "escalated_to_daniel" in schema


def test_continuation_after_skippy_escalation_advises_daniel(tmp_path, monkeypatch):
    """Once Hex has already routed a recurring class to Skippy (marked on the
    event row by sentinel-decide, simulated here directly), the next
    recurrence of that class advises Daniel instead -- the escalation ladder
    still never notifies from inside this deterministic tool."""
    db = tmp_path / "events.db"
    _init_db(db)
    monkeypatch.setattr(process_event, "DB_PATH", db)
    sent = _patch_notify(monkeypatch)

    last_event_id = None
    for minute in range(10):
        result = process_event.process(
            f"Sentinel alert at 2026-07-12T12:{minute:02d}:00Z. 1 anomalous log line.",
            occurred_at=f"2026-07-12T12:{minute:02d}:00Z",
            class_slug="unclassified_repeat_noise",
        )
        last_event_id = result["event_id"]
    assert result["recurrence_target"] == "skippy"

    # Simulate sentinel-decide having routed that last event to Skippy.
    conn = process_event._connect()
    conn.execute(
        "UPDATE events SET status = 'escalated_to_skippy' WHERE id = ?",
        (last_event_id,),
    )
    conn.commit()
    conn.close()

    result = process_event.process(
        "Sentinel alert at 2026-07-12T12:10:00Z. 1 anomalous log line.",
        occurred_at="2026-07-12T12:10:00Z",
        class_slug="unclassified_repeat_noise",
    )

    assert result["status"] == "awaiting_decision"
    assert result["recurrence_target"] == "daniel"
    assert sent == []


def test_recurrence_advisory_carries_eve_fields_for_the_caller(tmp_path, monkeypatch):
    """process_event.py never composes or sends the notification itself --
    it just stores the raw eve record on the payload so sentinel-decide can
    render the explicit alert line when it decides to notify."""
    db = tmp_path / "events.db"
    _init_db(db)
    monkeypatch.setattr(process_event, "DB_PATH", db)
    sent = _patch_notify(monkeypatch)
    eve = {
        "src_ip": "192.0.2.10",
        "src_port": 51514,
        "dest_ip": "192.0.2.20",
        "dest_port": 3389,
        "proto": "tcp",
        "alert": {"signature": "ET SCAN RDP inbound"},
    }

    for minute in range(9):
        process_event.process(
            "Sentinel alert at 2026-07-12T12:00:00Z. 1 anomalous log line.",
            occurred_at=f"2026-07-12T12:{minute:02d}:00Z",
            class_slug="unclassified_repeat_noise",
        )

    result = process_event.process(
        "Sentinel alert at 2026-07-12T12:09:30Z. 1 anomalous log line.\n"
        + json.dumps(eve),
        occurred_at="2026-07-12T12:09:30Z",
        class_slug="unclassified_repeat_noise",
    )

    assert result["status"] == "awaiting_decision"
    assert result["recurrence_target"] == "skippy"
    assert result["payload"]["eve"]["src_ip"] == "192.0.2.10"
    assert result["payload"]["eve"]["alert"]["signature"] == "ET SCAN RDP inbound"
    assert sent == []


# ---------------------------------------------------------------------------
# _send_notify interpreter selection
# ---------------------------------------------------------------------------


def test_send_notify_prefers_service_python(tmp_path, monkeypatch):
    notify_script = tmp_path / "notify.py"
    notify_script.write_text("")
    service_python = tmp_path / "python3"
    service_python.write_text("")
    calls = []

    class Result:
        returncode = 0
        stdout = '{"delivered": true}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(process_event, "NOTIFY_SCRIPT", notify_script)
    monkeypatch.setattr(process_event, "SERVICE_PYTHON", service_python)
    monkeypatch.setattr(process_event.subprocess, "run", fake_run)

    ok, detail = process_event._send_notify("hello", ["telegram"])

    assert ok is True
    assert detail == '{"delivered": true}'
    assert calls[0][0] == str(service_python)


def test_send_notify_falls_back_to_current_python(tmp_path, monkeypatch):
    notify_script = tmp_path / "notify.py"
    notify_script.write_text("")
    calls = []

    class Result:
        returncode = 0
        stdout = '{"delivered": true}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(process_event, "NOTIFY_SCRIPT", notify_script)
    monkeypatch.setattr(process_event, "SERVICE_PYTHON", tmp_path / "missing-python3")
    monkeypatch.setattr(process_event.subprocess, "run", fake_run)

    ok, _ = process_event._send_notify("hello", ["telegram"])

    assert ok is True
    assert calls[0][0] == sys.executable
