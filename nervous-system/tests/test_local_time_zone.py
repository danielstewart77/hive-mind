"""The nervous system keeps the machine's time, not one host's.

Comms stamps every message it hands a mind with the date and time, and
lucent's nightly prune fires on a cron read in a zone. Both used to assume
America/Chicago; an installed hive sets `TZ` from the machine it runs on,
and a host that sets nothing keeps Chicago.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import server
from comms import sessions


def test_message_stamp_is_in_the_zone_tz_names(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert sessions._local_zone() == ZoneInfo("Europe/Berlin")


def test_message_stamp_without_tz_is_in_chicago(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    assert sessions._local_zone() == ZoneInfo("America/Chicago")


def test_blank_tz_stamps_in_chicago_rather_than_failing(monkeypatch):
    """Compose renders `TZ: ${TZ}` on a host exporting none as an empty
    string, and ZoneInfo("") raises — on every message."""
    monkeypatch.setenv("TZ", "")
    assert sessions._local_zone() == ZoneInfo("America/Chicago")


def test_unknown_tz_stamps_in_chicago_rather_than_failing(monkeypatch):
    monkeypatch.setenv("TZ", "Nowhere/Atlantis")
    assert sessions._local_zone() == ZoneInfo("America/Chicago")


def test_prune_runs_in_the_zone_tz_names(monkeypatch):
    monkeypatch.delenv("PRUNE_TIMEZONE", raising=False)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert server.prune_timezone() == "Asia/Tokyo"


def test_prune_timezone_overrides_tz(monkeypatch):
    """A host that set PRUNE_TIMEZONE keeps its schedule."""
    monkeypatch.setenv("PRUNE_TIMEZONE", "America/Chicago")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert server.prune_timezone() == "America/Chicago"


def test_prune_without_any_zone_runs_in_chicago(monkeypatch):
    monkeypatch.setenv("PRUNE_TIMEZONE", "")
    monkeypatch.delenv("TZ", raising=False)
    assert server.prune_timezone() == "America/Chicago"


def test_prune_passes_over_an_unknown_zone(monkeypatch):
    """An unknown zone would have APScheduler refuse the job, and the prune
    would never run."""
    monkeypatch.setenv("PRUNE_TIMEZONE", "Nowhere/Atlantis")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert server.prune_timezone() == "Asia/Tokyo"
