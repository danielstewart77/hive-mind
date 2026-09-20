"""Tests for the Bitcoin alert gate — the hourly check that decides whether
Ada is woken at all, and the only thing keeping btc-ledger's record continuous
now that the skill runs a handful of times a year.

Each test names the requirement it protects.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
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


def _deep(tmp_path: Path) -> str:
    """Half the 200-day mean, 60% below the high, deep fear: every primary
    signal is well past its threshold."""
    return _fixture(tmp_path, "deep.json", current=40_000.0, high=100_000.0, fg=10)


def _flat(tmp_path: Path) -> str:
    """Trading at the high, on greed: nothing is firing."""
    return _fixture(tmp_path, "flat.json", current=100_000.0, high=100_000.0, fg=80)


def _state(tmp_path: Path, last_tier: str) -> str:
    path = tmp_path / "btc_buy_state.json"
    path.write_text(json.dumps({"last_tier": last_tier, "last_alert_at": 0}))
    return str(path)


# ---------------------------------------------------------------------------
# R8 — the gate fires on a signal and stays quiet without one
# ---------------------------------------------------------------------------
def test_gate_fires_when_the_evaluator_finds_a_signal(gate, tmp_path, monkeypatch):
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)

    result = gate.evaluate(None, _deep(tmp_path))

    assert gate.exit_code_for(result) == 10


def test_gate_stays_quiet_when_no_signal_is_present(gate, tmp_path, monkeypatch):
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)

    result = gate.evaluate(None, _flat(tmp_path))

    assert result["tier"] == "none"
    assert gate.exit_code_for(result) == 0


def test_gate_stays_quiet_during_quiet_hours_even_on_a_deep_signal(gate, tmp_path, monkeypatch):
    """Between 11pm and 6am Ada is not woken, however far the market has fallen.

    The tier is real and the gate still declines to fire, which is the only
    way to tell a gate reading the evaluator's decision from one reading the
    raw tier.
    """
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: True)

    result = gate.evaluate(None, _deep(tmp_path))

    assert result["tier"] != "none"
    assert gate.exit_code_for(result) == 0


def test_gate_stays_quiet_when_the_latch_already_alerted_at_this_tier(gate, tmp_path, monkeypatch):
    """The latch is read, not ignored — otherwise one deep drop alerts hourly
    until the price recovers."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    deep = _deep(tmp_path)
    fresh = gate.evaluate(_state(tmp_path, "none"), deep)

    latched = gate.evaluate(_state(tmp_path, fresh["tier"]), deep)

    assert gate.exit_code_for(fresh) == 10
    assert gate.exit_code_for(latched) == 0


def test_gate_never_raises_the_latch_itself(gate, tmp_path, monkeypatch):
    """Raising it is the mind's run — a gate that latched an alert it then
    failed to deliver would swallow it."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    state = _state(tmp_path, "none")
    deep = _deep(tmp_path)

    result = gate.evaluate(state, deep)
    gate.lower_latch_if_deescalated(state, result)

    assert json.loads(Path(state).read_text())["last_tier"] == "none"


def test_gate_walks_the_latch_down_when_the_market_recovers(gate, tmp_path, monkeypatch):
    """Without this the latch pins at the deepest tier ever seen and the
    alerter goes silent for good — which is exactly what it did between June
    and September 2026."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    state = _state(tmp_path, "deep_value")
    recovered = _fixture(tmp_path, "mild.json", current=100_000.0, high=100_000.0, fg=80)

    result = gate.evaluate(state, recovered)
    moved = gate.lower_latch_if_deescalated(state, result)

    assert moved is True
    assert json.loads(Path(state).read_text())["last_tier"] == "none"


def test_walking_the_latch_down_does_not_rewrite_when_daniel_was_last_told(
    gate, tmp_path, monkeypatch,
):
    """A recovery is not an alert."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    path = tmp_path / "btc_buy_state.json"
    path.write_text(json.dumps({"last_tier": "deep_value", "last_alert_at": 1781233209}))
    recovered = _fixture(tmp_path, "mild.json", current=100_000.0, high=100_000.0, fg=80)

    result = gate.evaluate(str(path), recovered)
    gate.lower_latch_if_deescalated(str(path), result)

    assert json.loads(path.read_text())["last_alert_at"] == 1781233209


def test_a_recovered_market_can_alert_again_after_the_latch_walks_down(
    gate, tmp_path, monkeypatch,
):
    """The whole point: the alerter has to be able to speak a second time."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    state = _state(tmp_path, "deep_value")
    recovered = _fixture(tmp_path, "mild.json", current=100_000.0, high=100_000.0, fg=80)
    gate.lower_latch_if_deescalated(state, gate.evaluate(state, recovered))

    second_fall = gate.evaluate(state, _deep(tmp_path))

    assert gate.exit_code_for(second_fall) == 10


def test_quiet_hours_leave_the_latch_alone(gate, tmp_path, monkeypatch):
    """An escalation buffered overnight must survive the night."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: True)
    state = _state(tmp_path, "deep_value")
    recovered = _fixture(tmp_path, "mild.json", current=100_000.0, high=100_000.0, fg=80)

    moved = gate.lower_latch_if_deescalated(state, gate.evaluate(state, recovered))

    assert moved is False
    assert json.loads(Path(state).read_text())["last_tier"] == "deep_value"


# ---------------------------------------------------------------------------
# R9 — every hour records its observation, quiet or not
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "quiet_hours, fixture_name, expected_code",
    [(True, "deep", 0), (False, "flat", 0), (False, "deep", 10)],
)
def test_every_run_posts_the_hourly_observation(
    gate, tmp_path, monkeypatch, quiet_hours, fixture_name, expected_code,
):
    """The dashboard's record is continuous only if the firing hours are in it
    too — those are the ones worth plotting."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: quiet_hours)
    path = _deep(tmp_path) if fixture_name == "deep" else _flat(tmp_path)
    monkeypatch.setattr(sys, "argv", ["btc_gate.py", "--test-fixture", path, "--ledger"])

    posted: list[dict] = []
    monkeypatch.setattr(gate, "post_observation", lambda result: posted.append(result))

    code = gate.main()

    assert code == expected_code
    assert len(posted) == 1


def test_observation_carries_this_run_s_readings_to_the_ledger(gate, monkeypatch):
    """The payload is what the dashboard plots, so it is asserted on the wire."""
    sent: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

    def _fake_post(url, json=None, headers=None, timeout=None):
        sent["url"] = url
        sent["payload"] = json
        sent["headers"] = headers
        return _Resp()

    monkeypatch.setitem(sys.modules, "requests", type("R", (), {"post": staticmethod(_fake_post)}))
    monkeypatch.setitem(
        sys.modules, "core.secrets",
        type("S", (), {"get_credential": staticmethod(
            lambda k: "ledger-tok" if k == "BTC_LEDGER_API_TOKEN" else None
        )}),
    )
    when = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)

    gate.post_observation({
        "timestamp": when.isoformat(),
        "price_usd": 61_234.5,
        "ath_usd": 100_000.0,
        "drawdown_pct": 38.77,
        "ma_200d": 80_000.0,
        "mayer_multiple": 0.7654,
        "fear_greed": 12,
        "fear_greed_classification": "Extreme Fear",
    })

    assert sent["url"].endswith("/observations")
    assert sent["payload"]["timestamp"] == int(when.timestamp())
    assert sent["payload"]["price_usd"] == 61_234.5
    assert sent["payload"]["mayer_multiple"] == 0.7654
    assert sent["payload"]["fear_greed"] == 12
    assert sent["payload"]["source"] == "live"
    assert sent["headers"]["Authorization"] == "Bearer ledger-tok"


def test_a_ledger_refusal_is_raised_rather_than_read_as_success(gate, monkeypatch):
    """A 500 that looked like a write is how a gap appears in a record nobody
    is watching."""
    class _Refused:
        def raise_for_status(self):
            raise RuntimeError("500 Server Error")

    monkeypatch.setitem(
        sys.modules, "requests",
        type("R", (), {"post": staticmethod(lambda *a, **k: _Refused())}),
    )
    monkeypatch.setitem(
        sys.modules, "core.secrets",
        type("S", (), {"get_credential": staticmethod(lambda k: "tok")}),
    )

    with pytest.raises(RuntimeError):
        gate.post_observation({"timestamp": "2026-09-20T14:00:00+00:00", "price_usd": 1.0})


def test_a_failed_observation_is_reported_on_a_quiet_hour_and_not_on_a_firing_one(
    gate, tmp_path, monkeypatch,
):
    """An alert outranks the ledger; a quiet hour has nothing else at stake."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)

    def _boom(result):
        raise RuntimeError("ledger unreachable")

    monkeypatch.setattr(gate, "post_observation", _boom)

    monkeypatch.setattr(sys, "argv", ["btc_gate.py", "--test-fixture", _flat(tmp_path), "--ledger"])
    quiet_hour_code = gate.main()

    monkeypatch.setattr(sys, "argv", ["btc_gate.py", "--test-fixture", _deep(tmp_path), "--ledger"])
    firing_hour_code = gate.main()

    assert quiet_hour_code == 1
    assert firing_hour_code == 10


def test_a_fixture_run_records_nothing_in_the_ledger(gate, tmp_path, monkeypatch):
    """Hand-verifying the gate must not leave fabricated prices on the
    dashboard stamped as live readings."""
    monkeypatch.setattr(gate.btc_signals, "in_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", ["btc_gate.py", "--test-fixture", _deep(tmp_path)])

    posted: list[dict] = []
    monkeypatch.setattr(gate, "post_observation", lambda result: posted.append(result))

    code = gate.main()

    assert code == 10
    assert posted == []


def test_an_evaluator_that_fails_reports_broken_rather_than_quiet(gate, tmp_path, monkeypatch):
    """Exit zero means "nothing to say". A gate that cannot read the market has
    something to say."""
    monkeypatch.setattr(sys, "argv", ["btc_gate.py", "--test-fixture", str(tmp_path / "absent.json")])

    assert gate.main() == 1
