"""Tests for the Bitcoin alert gate — the hourly check that decides whether
Ada is woken at all, and the only thing keeping btc-ledger's record continuous
now that the skill runs a handful of times a year.

Each test names the requirement it protects.
"""

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

GATE_PATH = Path(__file__).resolve().parents[2] / "tools" / "stateless" / "btc_signals" / "btc_gate.py"


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("btc_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["btc_gate"] = module
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path, name: str, *, current: float, high: float, fg: int) -> str:
    """A year of daily prices flat at `high`, with today at `current`.

    The 200-day mean and the all-time high therefore both come out of the
    real evaluator rather than being asserted here.
    """
    prices = [[i * 86_400_000, high] for i in range(364)] + [[364 * 86_400_000, current]]
    path = tmp_path / name
    path.write_text(json.dumps({"prices": prices, "fear_greed": fg}))
    return str(path)


# ---------------------------------------------------------------------------
# R8 — the gate fires on a signal and stays quiet without one
# ---------------------------------------------------------------------------
def test_gate_fires_when_the_evaluator_finds_a_signal(gate, tmp_path, monkeypatch):
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    # Half the 200-day mean, 60% below the high, deep fear: every primary
    # signal is well past its threshold.
    deep = _fixture(tmp_path, "deep.json", current=40_000.0, high=100_000.0, fg=10)

    result = gate.evaluate(None, deep)

    assert result["tier"] != "none"
    assert gate.exit_code_for(result) == 10


def test_gate_stays_quiet_when_no_signal_is_present(gate, tmp_path, monkeypatch):
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    # Trading at the high, on greed: nothing is firing.
    flat = _fixture(tmp_path, "flat.json", current=100_000.0, high=100_000.0, fg=80)

    result = gate.evaluate(None, flat)

    assert result["tier"] == "none"
    assert gate.exit_code_for(result) == 0


def test_gate_leaves_the_latch_for_the_mind_to_move(gate, tmp_path, monkeypatch):
    """A gate that latched would swallow an alert whose delivery then failed."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    state = tmp_path / "btc_buy_state.json"
    state.write_text(json.dumps({"last_tier": "none", "last_alert_at": 0}))
    deep = _fixture(tmp_path, "deep.json", current=40_000.0, high=100_000.0, fg=10)

    gate.evaluate(str(state), deep)

    assert json.loads(state.read_text())["last_tier"] == "none"


# ---------------------------------------------------------------------------
# R9 — a quiet hour still records the observation
# ---------------------------------------------------------------------------
def test_quiet_hour_still_posts_the_hourly_observation(gate, tmp_path, monkeypatch):
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    flat = _fixture(tmp_path, "flat.json", current=100_000.0, high=100_000.0, fg=80)
    monkeypatch.setattr(sys, "argv", ["btc_gate.py", "--test-fixture", flat])

    posted: list[dict] = []
    monkeypatch.setattr(gate, "post_observation", lambda result: posted.append(result))

    code = gate.main()

    assert code == 0
    assert len(posted) == 1
    assert posted[0]["price_usd"] == 100_000.0


def test_observation_carries_this_run_s_readings_to_the_ledger(gate, tmp_path, monkeypatch):
    """The payload is what the dashboard plots, so it is asserted on the wire."""
    sent: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

    def _fake_post(url, json=None, headers=None, timeout=None):
        sent["url"] = url
        sent["payload"] = json
        sent["headers"] = headers
        return _Resp()

    fake_requests = type("R", (), {"post": staticmethod(_fake_post)})
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setitem(
        sys.modules, "core.secrets",
        type("S", (), {"get_credential": staticmethod(lambda k: "ledger-tok")}),
    )

    gate.post_observation({
        "timestamp": datetime(2026, 9, 20, 14, 0, tzinfo=None).isoformat(),
        "price_usd": 61_234.5,
        "ath_usd": 100_000.0,
        "drawdown_pct": 38.77,
        "ma_200d": 80_000.0,
        "mayer_multiple": 0.7654,
        "fear_greed": 12,
        "fear_greed_classification": "Extreme Fear",
    })

    assert sent["url"].endswith("/observations")
    assert sent["payload"]["price_usd"] == 61_234.5
    assert sent["payload"]["mayer_multiple"] == 0.7654
    assert sent["payload"]["source"] == "live"
    assert sent["headers"]["Authorization"] == "Bearer ledger-tok"
