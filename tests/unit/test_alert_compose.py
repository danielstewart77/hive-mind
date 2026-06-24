"""Guards for the explicit alert-fields composer in event_triage.

The Sentinel backlog "Alert format (the last-leg fix)" requires every
Daniel-facing alert to lead with an explicit one-line story that states
source IP, source port, destination IP, destination port, and protocol —
never a vague "RDP alert" with no pivot facts. These tests lock in the two
helpers that produce that line (`compose_alert_fields` extracts the six
fields from a raw Suricata eve record; `render_alert_line` renders the
single explicit line) and guard that the existing condition matchers
(`src_in:`, `excerpt_contains:`) are not regressed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tools.stateless.event_triage import process_event


# Representative full Suricata eve alert record.
FULL_EVE = {
    "event_type": "alert",
    "src_ip": "194.165.16.165",
    "src_port": 65441,
    "dest_ip": "203.0.113.10",
    "dest_port": 443,
    "proto": "TCP",
    "alert": {"signature": "RDP-fingerprint scan"},
}


def test_compose_alert_fields_full_eve() -> None:
    """All six fields are extracted and correctly typed from a full record."""
    fields = process_event.compose_alert_fields(FULL_EVE)
    assert fields["src_ip"] == "194.165.16.165"
    assert fields["src_port"] == 65441
    assert fields["dst_ip"] == "203.0.113.10"
    assert fields["dst_port"] == 443
    assert fields["proto"] == "TCP"
    assert fields["signature"] == "RDP-fingerprint scan"
    # ports are integers, ips/proto/signature strings
    assert isinstance(fields["src_port"], int)
    assert isinstance(fields["dst_port"], int)


def test_render_alert_line_explicit() -> None:
    """The rendered line carries src ip+port, dst ip+port, protocol, in shape."""
    fields = process_event.compose_alert_fields(FULL_EVE)
    line = process_event.render_alert_line(fields)
    assert "194.165.16.165:65441" in line
    assert "203.0.113.10:443" in line
    assert "→" in line
    assert "proto TCP" in line
    assert "RDP-fingerprint scan" in line


def test_render_today_example() -> None:
    """The example names all four pivot facts in the explicit line."""
    line = process_event.render_alert_line(
        process_event.compose_alert_fields(FULL_EVE)
    )
    # The four pivot facts must all be present.
    for token in ("194.165.16.165", "65441", "203.0.113.10", "443", "TCP"):
        assert token in line


def test_missing_fields_render_question_mark() -> None:
    """Missing dest_port / proto render as '?' and are never silently dropped."""
    partial = {
        "src_ip": "10.0.0.5",
        "src_port": 1234,
        "dest_ip": "10.0.0.9",
        # no dest_port, no proto, no alert
    }
    fields = process_event.compose_alert_fields(partial)
    assert fields["dst_port"] is None
    assert fields["proto"] is None
    line = process_event.render_alert_line(fields)
    # src present verbatim
    assert "10.0.0.5:1234" in line
    # missing dst_port and proto show as '?', not dropped
    assert "10.0.0.9:?" in line
    assert "proto ?" in line


def test_existing_condition_matchers_unregressed() -> None:
    """`src_in:` and `excerpt_contains:` still behave after the file edit."""
    # src_in: matches when the alert source IP is inside the CIDR
    payload_in = {"excerpt": "alert 203.0.113.7 to 203.0.113.10 RDP scan"}
    assert process_event._condition_matches("src_in:203.0.113.0/24", payload_in)
    assert not process_event._condition_matches("src_in:10.0.0.0/8", payload_in)

    # excerpt_contains: matches the literal substring case-insensitively
    payload_x = {"excerpt": "Caddy reverse-proxy 502 Bad Gateway"}
    assert process_event._condition_matches("excerpt_contains:bad gateway", payload_x)
    assert not process_event._condition_matches("excerpt_contains:timeout", payload_x)

    # legacy rules (None / always) still match everything
    assert process_event._condition_matches(None, {})
    assert process_event._condition_matches("always", {})
