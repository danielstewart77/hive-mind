"""Unit tests for the stateless btc_signals tool.

Covers the pure-math signal/tier/latch logic and end-to-end behavior via
the test-fixture path.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = _PROJECT_ROOT / "tools/stateless/btc_signals/btc_signals.py"


# ---------------------------------------------------------------------------
# Import the module directly so we can unit-test pure functions.
# ---------------------------------------------------------------------------

def _load_module():
    spec = importlib.util.spec_from_file_location("btc_signals", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


btc = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _history(prices: list[float]) -> dict:
    """Build a price history dict from a list of daily closes."""
    return {
        "prices": [[i * 86_400_000, p] for i, p in enumerate(prices)],
        "current": prices[-1],
    }


def _flat_history(price: float, days: int = 250) -> dict:
    return _history([price] * days)


# ---------------------------------------------------------------------------
# Tier decision matrix
# ---------------------------------------------------------------------------

def test_tier_none_when_no_signals():
    history = _flat_history(120_000.0)
    signals = btc.compute_signals(history, {"value": 70, "classification": "Greed"})
    assert btc.decide_tier(signals) == "none"


def test_tier_opportunistic_on_mayer_only():
    prices = [100_000.0] * 199 + [95_000.0]
    history = _history(prices)
    signals = btc.compute_signals(history, {"value": 50, "classification": "Neutral"})
    # Mayer should be just under 1.0 — opportunistic only, no other signals.
    assert signals["mayer_multiple"] < btc.MAYER_OPPORTUNISTIC
    assert signals["mayer_multiple"] > btc.MAYER_STRONG
    assert btc.decide_tier(signals) == "opportunistic"


def test_tier_deep_value_today():
    """Reproduce the live state at 2026-06-06: Mayer 0.77, drawdown 51%, F&G 12."""
    prices = [124_774.0] + [78_000.0] * 199 + [60_684.0]
    history = _history(prices)
    signals = btc.compute_signals(history, {"value": 12, "classification": "Extreme Fear"})
    assert btc.decide_tier(signals) == "deep_value"


def test_tier_generational_requires_all_three_deepest():
    prices = [200_000.0] + [180_000.0] * 199 + [80_000.0]
    history = _history(prices)
    signals = btc.compute_signals(history, {"value": 10, "classification": "Extreme Fear"})
    # Mayer ~0.44, drawdown 60%, F&G 10 — all generational-grade.
    assert signals["mayer_multiple"] < btc.MAYER_GENERATIONAL
    assert signals["drawdown_pct"] / 100 > btc.DRAWDOWN_GENERATIONAL
    assert signals["fear_greed"] < btc.FG_GENERATIONAL
    assert btc.decide_tier(signals) == "generational"


def test_tier_strong_on_two_stacked():
    """Mayer 0.93 (opportunistic) + F&G 22 (opportunistic) stack → strong."""
    prices = [100_000.0] * 199 + [93_000.0]
    history = _history(prices)
    signals = btc.compute_signals(history, {"value": 22, "classification": "Fear"})
    active = btc.signals_active(signals)
    assert "mayer" in active
    assert "fear_greed" in active
    assert btc.decide_tier(signals) == "strong"


# ---------------------------------------------------------------------------
# Signal math
# ---------------------------------------------------------------------------

def test_drawdown_zero_at_ath():
    history = _flat_history(100_000.0)
    signals = btc.compute_signals(history, {"value": 50, "classification": "Neutral"})
    assert signals["drawdown_pct"] == pytest.approx(0.0, abs=1e-6)


def test_mayer_multiple_one_when_flat():
    history = _flat_history(50_000.0)
    signals = btc.compute_signals(history, {"value": 50, "classification": "Neutral"})
    assert signals["mayer_multiple"] == pytest.approx(1.0, abs=1e-6)


def test_signals_active_returns_only_firing():
    history = _flat_history(60_000.0)
    signals = btc.compute_signals(history, {"value": 50, "classification": "Neutral"})
    # Flat-price history → Mayer 1.0, drawdown 0, F&G neutral. Nothing fires.
    assert btc.signals_active(signals) == []


# ---------------------------------------------------------------------------
# Buy-size mapping
# ---------------------------------------------------------------------------

def test_buy_size_per_tier():
    assert btc.BUY_SIZE_USD["none"] == 0
    assert btc.BUY_SIZE_USD["opportunistic"] == 150
    assert btc.BUY_SIZE_USD["strong"] == 300
    assert btc.BUY_SIZE_USD["deep_value"] == 500
    assert btc.BUY_SIZE_USD["generational"] == 750


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------

def test_quiet_hours_at_midnight_central():
    midnight = datetime(2026, 6, 6, 0, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert btc.in_quiet_hours(midnight) is True


def test_quiet_hours_at_eleven_pm_central():
    eleven_pm = datetime(2026, 6, 6, 23, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert btc.in_quiet_hours(eleven_pm) is True


def test_active_hours_at_six_am_central():
    six_am = datetime(2026, 6, 6, 6, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert btc.in_quiet_hours(six_am) is False


def test_active_hours_at_ten_pm_central():
    ten_pm = datetime(2026, 6, 6, 22, 30, tzinfo=ZoneInfo("America/Chicago"))
    assert btc.in_quiet_hours(ten_pm) is False


# ---------------------------------------------------------------------------
# Latch / alert decision
# ---------------------------------------------------------------------------

def test_alert_on_first_escalation():
    fired, reason = btc.decide_alert("deep_value", "none", quiet=False)
    assert fired is True
    assert "escalation" in reason


def test_no_alert_on_same_tier():
    fired, _ = btc.decide_alert("deep_value", "deep_value", quiet=False)
    assert fired is False


def test_no_alert_on_de_escalation():
    fired, _ = btc.decide_alert("opportunistic", "deep_value", quiet=False)
    assert fired is False


def test_no_alert_during_quiet_hours_even_on_escalation():
    fired, reason = btc.decide_alert("deep_value", "none", quiet=True)
    assert fired is False
    assert "quiet" in reason


def test_no_alert_when_tier_is_none():
    fired, _ = btc.decide_alert("none", "none", quiet=False)
    assert fired is False


def test_escalation_from_lower_tier_alerts():
    fired, reason = btc.decide_alert("deep_value", "opportunistic", quiet=False)
    assert fired is True
    assert "opportunistic" in reason and "deep_value" in reason


# ---------------------------------------------------------------------------
# State file read/write
# ---------------------------------------------------------------------------

def test_read_state_missing_file_returns_defaults(tmp_path):
    state = btc.read_state(tmp_path / "absent.json")
    assert state["last_tier"] == "none"
    assert state["last_alert_at"] == 0


def test_write_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    btc.write_state(path, {"last_tier": "deep_value", "last_alert_at": 12345})
    loaded = btc.read_state(path)
    assert loaded["last_tier"] == "deep_value"
    assert loaded["last_alert_at"] == 12345


def test_read_state_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("not valid json {{{")
    state = btc.read_state(path)
    assert state["last_tier"] == "none"


# ---------------------------------------------------------------------------
# End-to-end via subprocess with a test fixture (no network)
# ---------------------------------------------------------------------------

def test_subprocess_with_fixture_returns_valid_json(tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "prices": [[i * 86_400_000, 100_000.0] for i in range(200)] + [[200 * 86_400_000, 60_000.0]],
        "fear_greed": 12,
    }))
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--test-fixture", str(fixture),
         "--ignore-quiet-hours"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["tier"] in btc.TIER_ORDER
    assert "suggested_buy_usd" in data
    assert isinstance(data["should_alert"], bool)


def test_subprocess_persists_state_on_alert(tmp_path):
    state_file = tmp_path / "state.json"
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "prices": [[i * 86_400_000, 100_000.0] for i in range(200)] + [[200 * 86_400_000, 60_000.0]],
        "fear_greed": 12,
    }))
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--test-fixture", str(fixture),
         "--state-file", str(state_file),
         "--update-state",
         "--ignore-quiet-hours"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["should_alert"] is True
    assert state_file.exists()
    persisted = json.loads(state_file.read_text())
    assert persisted["last_tier"] == data["tier"]


def test_subprocess_does_not_re_alert_at_same_tier(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_tier": "deep_value", "last_alert_at": 100}))
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "prices": [[i * 86_400_000, 100_000.0] for i in range(200)] + [[200 * 86_400_000, 60_000.0]],
        "fear_greed": 12,
    }))
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--test-fixture", str(fixture),
         "--state-file", str(state_file),
         "--update-state",
         "--ignore-quiet-hours"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["tier"] == "deep_value"
    assert data["should_alert"] is False


def test_subprocess_quiet_hours_suppresses_state_update(tmp_path):
    """During quiet hours, latch must NOT be updated so morning fires the alert."""
    state_file = tmp_path / "state.json"
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "prices": [[i * 86_400_000, 100_000.0] for i in range(200)] + [[200 * 86_400_000, 60_000.0]],
        "fear_greed": 12,
    }))
    # Don't ignore quiet hours. Whether we're in quiet hours depends on
    # wall clock — but regardless, this test asserts: if the tool decides
    # not to alert (quiet OR no signals), state file is not created.
    # We force "quiet" by NOT passing --ignore-quiet-hours and checking
    # the result's quiet_hours_active flag.
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH),
         "--test-fixture", str(fixture),
         "--state-file", str(state_file),
         "--update-state"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    if data["quiet_hours_active"]:
        # During quiet hours, alert is suppressed but tier is still computed.
        # State file should NOT exist because we never alerted and the tier
        # is not "none".
        assert data["should_alert"] is False
        assert not state_file.exists()
    else:
        # Active hours: deep_value triggered, alert fired, state written.
        assert data["should_alert"] is True
        assert state_file.exists()
