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
import urllib.parse
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = _PROJECT_ROOT / "bots/scheduled_tasks/scripts/netsage_run.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("netsage_run", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


netsage = _load_module()


def _eve_record(event_type="alert", severity=1, signature="ET ATTACK Something",
                category="A Network Trojan was Detected", src="10.0.0.5",
                dst="10.0.0.1"):
    """The real Suricata eve.json record — what lives INSIDE the envelope's
    nested `message` string, where event_type, alert{}, and src/dest_ip sit."""
    record = {
        "timestamp": "2026-06-28T19:45:05.562147-0500",
        "event_type": event_type,
        "in_iface": "enp9s0",
        "pkt_src": "wire/pcap",
        "src_ip": src,
        "dest_ip": dst,
    }
    if event_type == "alert":
        record["alert"] = {
            "action": "allowed",
            "severity": severity,
            "signature": signature,
            "category": category,
        }
    return record


def _suricata_eve(host="sentinel", **kw):
    """The full Loki value Vector ships for a Suricata eve line.

    Vector tails eve.json and ships each line as a JSON *string* in the
    envelope's `message` field — it does NOT flatten eve fields to the top
    level. event_type and host ride as envelope fields/labels; the alert object
    only exists inside the nested string. This is the byte shape Loki returns.
    """
    inner = _eve_record(**kw)
    envelope = {
        "event_type": inner["event_type"],
        "file": "/var/log/suricata/eve.json",
        "host": host,
        "message": json.dumps(inner),
        "source_type": "file",
    }
    return json.dumps(envelope)


# ---------------------------------------------------------------------------
# _suricata_alert
# ---------------------------------------------------------------------------

def test_high_severity_alert_is_surfaced():
    record = _eve_record(severity=1, signature="ET ATTACK X")
    line = netsage._suricata_alert(record)
    assert line is not None
    assert "severity 1" in line
    assert "ET ATTACK X" in line
    assert "10.0.0.5 to 10.0.0.1" in line


def test_ceiling_severity_alert_is_surfaced():
    record = _eve_record(severity=netsage.SURICATA_SEVERITY_CEILING)
    assert netsage._suricata_alert(record) is not None


def test_informational_severity_alert_is_dropped():
    record = _eve_record(severity=3, signature="SURICATA Ethertype unknown")
    assert netsage._suricata_alert(record) is None


def test_non_alert_suricata_event_is_dropped():
    record = _eve_record(event_type="flow")
    assert netsage._suricata_alert(record) is None


def test_alert_without_severity_still_surfaces():
    record = _eve_record()
    del record["alert"]["severity"]
    line = netsage._suricata_alert(record)
    assert line is not None
    assert "severity ?" in line


def test_non_suricata_record_returns_none():
    assert netsage._suricata_alert({"message": "some app log"}) is None
    assert netsage._suricata_alert(None) is None


# ---------------------------------------------------------------------------
# _eve_record — the envelope-unwrap step that the false-clean bug skipped
# ---------------------------------------------------------------------------

def test_eve_record_unwraps_nested_message():
    envelope = json.loads(_suricata_eve(severity=1, signature="ET ATTACK X"))
    # The alert object is NOT on the envelope — only inside the nested string.
    assert "alert" not in envelope
    eve = netsage._eve_record(envelope)
    assert eve["event_type"] == "alert"
    assert eve["alert"]["severity"] == 1
    assert eve["alert"]["signature"] == "ET ATTACK X"


def test_eve_record_falls_back_to_envelope_without_message():
    flat = {"event_type": "alert", "alert": {"severity": 1, "signature": "X"}}
    assert netsage._eve_record(flat) is flat
    assert netsage._eve_record(None) is None


# ---------------------------------------------------------------------------
# pick_anomalies integration
# ---------------------------------------------------------------------------

def test_pick_anomalies_surfaces_suricata_alert():
    # Regression for the false clean: the eve alert lives in the nested message
    # string, not at the envelope top level. pick_anomalies must unwrap it or
    # every real severity 1/2 alert is silently dropped.
    lines = [("suricata", "unknown_service", "1", _suricata_eve(severity=1, signature="ET ATTACK X"))]
    out = netsage.pick_anomalies(lines)
    assert len(out) == 1
    label, _ts, msg = out[0]
    assert label == "suricata@sentinel"
    assert "ET ATTACK X" in msg
    assert "10.0.0.5 to 10.0.0.1" in msg


def test_pick_anomalies_drops_suricata_noise():
    lines = [
        ("suricata", "unknown_service", "1", _suricata_eve(severity=3, signature="SURICATA Ethertype unknown")),
        ("suricata", "unknown_service", "2", _suricata_eve(event_type="dns")),
        ("suricata", "unknown_service", "3", _suricata_eve(event_type="flow")),
    ]
    assert netsage.pick_anomalies(lines) == []


def test_pick_anomalies_drops_real_suricata_stats():
    # Regression: real eve.json stats lines embed crisis words ("error",
    # "invalid") in their counter names, nested in the envelope's message
    # string. The only Suricata tell is the Loki stream's source="suricata"
    # label; gating on a body field (the old bug) paged on every fire.
    inner = json.dumps({
        "timestamp": "2026-06-28T19:17:39.500920-0500",
        "event_type": "stats",
        "stats": {"app_layer": {"error": {"http": {"alloc": 0}}}, "capture": {"errors": 0}},
    })
    stats = json.dumps({
        "event_type": "stats",
        "file": "/var/log/suricata/eve.json",
        "host": "sentinel",
        "message": inner,
        "source_type": "file",
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
    assert labels == {"suricata@sentinel", "skippy.service"}


# ---------------------------------------------------------------------------
# pull_loki_lines — two-query split that keeps Suricata's sev-3 flood from
# blowing the line budget and burying real alerts / other services' errors
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_pull_loki_lines_runs_two_filtered_queries_and_merges(monkeypatch):
    captured = []

    def fake_urlopen(url, timeout=0):
        decoded = urllib.parse.unquote(url)
        captured.append(decoded)
        if 'source!="suricata"' in decoded:
            payload = {"data": {"result": [
                {"stream": {"source": "journald", "service_name": "ollama.service"},
                 "values": [["1", "CRITICAL: model failed to load"]]}
            ]}}
        else:
            payload = {"data": {"result": [
                {"stream": {"source": "suricata", "service_name": "unknown_service",
                            "event_type": "alert"},
                 "values": [["2", _suricata_eve(severity=1, signature="ET ATTACK X")]]}
            ]}}
        return _FakeResp(payload)

    monkeypatch.setattr(netsage.urllib.request, "urlopen", fake_urlopen)

    lines = netsage.pull_loki_lines()

    # Exactly two queries: generic (suricata excluded) and suricata-alert.
    assert len(captured) == 2
    assert any('source!="suricata"' in q for q in captured)
    suricata_q = next(q for q in captured if 'event_type="alert"' in q)
    assert "severity" in suricata_q, "suricata query must pre-filter severity at the Loki layer"

    # Lines from both queries merge, tagged by their stream source.
    assert {ln[0] for ln in lines} == {"journald", "suricata"}

    # End to end: both a non-suricata error and a nested suricata alert surface.
    labels = {label for label, _, _ in netsage.pick_anomalies(lines)}
    assert labels == {"ollama.service", "suricata@sentinel"}


def test_query_loki_returns_empty_on_failure(monkeypatch):
    def boom(url, timeout=0):
        raise OSError("loki unreachable")

    monkeypatch.setattr(netsage.urllib.request, "urlopen", boom)
    assert netsage._query_loki(netsage.GENERIC_QUERY) == []
