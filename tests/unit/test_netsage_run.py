"""Unit tests for the NetSage scheduler alert pass.

Focus is the anomaly picker, specifically that Suricata IDS alerts — which
express badness through a numeric severity field rather than the English
crisis words the generic regex hunts for — are surfaced, while informational
Suricata noise and non-alert Suricata events (flow/dns/tls) are dropped.

The picker identifies Suricata lines by the Loki stream's `source` label
(supplied by Vector), which is the first element of each input tuple
(source, service, ts, message). Real eve.json bodies carry no source marker
of their own, so gating on a body field silently leaked stats telemetry.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = _PROJECT_ROOT / "bots/scheduled_tasks/scripts/netsage_run.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("netsage_run", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


netsage = _load_module()


def _suricata_eve(event_type="alert", severity=1, signature="ET ATTACK Something",
                  category="A Network Trojan was Detected", src="10.0.0.5",
                  dst="10.0.0.1", host="NetSage"):
    """Build the JSON body Vector ships to Loki for a Suricata eve line.

    Real eve.json lines carry no source marker of their own — the "suricata"
    tag lives on the Loki stream's `source` label, supplied separately to
    pick_anomalies as the first tuple element.
    """
    record = {
        "event_type": event_type,
        "host": host,
        "src_ip": src,
        "dest_ip": dst,
        "in_iface": "enp2s0",
    }
    if event_type == "alert":
        record["alert"] = {
            "severity": severity,
            "signature": signature,
            "category": category,
            "action": "allowed",
        }
    return json.dumps(record)


# ---------------------------------------------------------------------------
# _suricata_alert
# ---------------------------------------------------------------------------

def test_high_severity_alert_is_surfaced():
    record = json.loads(_suricata_eve(severity=1, signature="ET ATTACK X"))
    line = netsage._suricata_alert(record)
    assert line is not None
    assert "severity 1" in line
    assert "ET ATTACK X" in line
    assert "10.0.0.5 to 10.0.0.1" in line


def test_ceiling_severity_alert_is_surfaced():
    record = json.loads(_suricata_eve(severity=netsage.SURICATA_SEVERITY_CEILING))
    assert netsage._suricata_alert(record) is not None


def test_informational_severity_alert_is_dropped():
    record = json.loads(_suricata_eve(severity=3, signature="SURICATA Ethertype unknown"))
    assert netsage._suricata_alert(record) is None


def test_non_alert_suricata_event_is_dropped():
    record = json.loads(_suricata_eve(event_type="flow"))
    assert netsage._suricata_alert(record) is None


def test_alert_without_severity_still_surfaces():
    record = json.loads(_suricata_eve())
    del record["alert"]["severity"]
    line = netsage._suricata_alert(record)
    assert line is not None
    assert "severity ?" in line


def test_non_suricata_record_returns_none():
    assert netsage._suricata_alert({"message": "some app log"}) is None
    assert netsage._suricata_alert(None) is None


# ---------------------------------------------------------------------------
# pick_anomalies integration
# ---------------------------------------------------------------------------

def test_pick_anomalies_surfaces_suricata_alert():
    lines = [("suricata", "unknown_service", "1", _suricata_eve(severity=1, signature="ET ATTACK X"))]
    out = netsage.pick_anomalies(lines)
    assert len(out) == 1
    label, _ts, msg = out[0]
    assert label == "suricata@NetSage"
    assert "ET ATTACK X" in msg


def test_pick_anomalies_drops_suricata_noise():
    lines = [
        ("suricata", "unknown_service", "1", _suricata_eve(severity=3, signature="SURICATA Ethertype unknown")),
        ("suricata", "unknown_service", "2", _suricata_eve(event_type="dns")),
        ("suricata", "unknown_service", "3", _suricata_eve(event_type="flow")),
    ]
    assert netsage.pick_anomalies(lines) == []


def test_pick_anomalies_drops_real_suricata_stats():
    # Regression: real eve.json stats lines carry NO source marker in the body
    # and their counter names embed crisis words ("error", "invalid"). The only
    # Suricata tell is the Loki stream's source="suricata" label. Gating on a
    # body field (the old bug) let these page Daniel every scheduler fire.
    stats = json.dumps({
        "timestamp": "2026-06-28T19:17:39.500920-0500",
        "event_type": "stats",
        "stats": {"app_layer": {"error": {"http": {"alloc": 0}}}, "capture": {"errors": 0}},
    })
    assert netsage.ANOMALY_REGEX.search(stats), "fixture must contain a crisis word to be a real regression"
    assert netsage.pick_anomalies([("suricata", "unknown_service", "1", stats)]) == []


def test_pick_anomalies_still_catches_plain_text_errors():
    lines = [("journald", "ollama.service", "1", "CRITICAL: model failed to load")]
    out = netsage.pick_anomalies(lines)
    assert len(out) == 1
    assert "CRITICAL" in out[0][2]


def test_pick_anomalies_mixed_stream():
    lines = [
        ("suricata", "unknown_service", "1", _suricata_eve(severity=1, signature="ET ATTACK X")),
        ("suricata", "unknown_service", "2", _suricata_eve(severity=3, signature="Ethertype unknown")),
        ("journald", "skippy.service", "3", "Traceback (most recent call last):"),
        ("suricata", "unknown_service", "4", _suricata_eve(event_type="tls")),
    ]
    out = netsage.pick_anomalies(lines)
    assert len(out) == 2
    labels = {label for label, _, _ in out}
    assert labels == {"suricata@NetSage", "skippy.service"}
