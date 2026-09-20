"""The cheap hourly check that decides whether Ada is woken about Bitcoin.

This runs on the scheduler's cron as a plain subprocess: no session, no
mind, no tool calls. Most hours it reads the market, writes one observation
to btc-ledger so the dashboard keeps a continuous record, and exits quiet.
Only when the evaluator says a signal is present does it exit with the
scheduler's fire code, and only then is Ada woken to look for herself.

It only ever moves the latch **down**. Raising it is the mind's own run's
job, so an alert decided here but never delivered — the gateway was down,
the turn failed — is re-decided next hour rather than swallowed by a latch
that moved with nobody listening. Lowering has to happen here, because the
mind is no longer woken on the hours when the market recovers: a latch that
can only rise is a latch that pins at the deepest tier ever seen and goes
silent for good. That is not hypothetical — it is how this alerter sat out
a 35% drawdown between June and September 2026.

Exit codes: 0 quiet, 10 fire, 1 broken. Anything but 0 and 10 is reported
to Daniel by the scheduler.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/usr/src/app")

import btc_signals  # noqa: E402

FIRE_EXIT_CODE = 10
QUIET_EXIT_CODE = 0
BROKEN_EXIT_CODE = 1

LEDGER_URL = os.environ.get("BTC_LEDGER_URL", "http://btc-ledger:8427")


def evaluate(state_file: str | None, fixture: str | None = None) -> dict:
    """Read the market and decide, using the same evaluator the mind runs.

    The state file is read for the latch's previous tier and never written.
    """
    if fixture:
        history, fg_value = btc_signals._load_test_data(fixture)
        fear_greed = {"value": fg_value, "classification": "Test"}
    else:
        history = btc_signals.fetch_btc_history()
        fear_greed = btc_signals.fetch_fear_greed()

    signals = btc_signals.compute_signals(history, fear_greed)
    active = btc_signals.signals_active(signals)
    tier = btc_signals.decide_tier(signals)
    quiet = btc_signals.in_quiet_hours()

    previous_tier = "none"
    if state_file:
        previous_tier = btc_signals.read_state(Path(state_file))["last_tier"]

    should_alert, reason = btc_signals.decide_alert(tier, previous_tier, quiet)
    return btc_signals.build_result(
        signals, tier, active, should_alert, reason, quiet, previous_tier
    )


def lower_latch_if_deescalated(state_file: str | None, result: dict) -> bool:
    """Walk the latch down to the current tier when the market has recovered.

    Never upward: that is the mind's run, and only a delivered alert should
    raise it. Quiet hours are left alone so a buffered escalation survives
    the night exactly as it did before.

    Returns whether the latch was moved.
    """
    if not state_file or result.get("quiet_hours_active"):
        return False
    current = result.get("tier", "none")
    previous = result.get("previous_tier", "none")
    if btc_signals.tier_rank(current) >= btc_signals.tier_rank(previous):
        return False
    path = Path(state_file)
    state = btc_signals.read_state(path)
    btc_signals.write_state(path, {
        "last_tier": current,
        # The recovery is not an alert, so the last time Daniel was actually
        # told something stays where it was.
        "last_alert_at": state.get("last_alert_at", 0),
    })
    return True


def exit_code_for(result: dict) -> int:
    """Fire only when the evaluator says an alert is warranted."""
    return FIRE_EXIT_CODE if result.get("should_alert") else QUIET_EXIT_CODE


def _epoch(result: dict) -> int:
    raw = str(result.get("timestamp", ""))
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return int(datetime.fromisoformat(raw).timestamp())
    except ValueError:
        import time
        return int(time.time())


def post_observation(result: dict) -> None:
    """Write this hour's reading to btc-ledger.

    The dashboard's record is continuous only if this happens on every run,
    including the quiet ones — which is precisely why it lives here and not
    in the skill, since the skill now runs a handful of times a year.
    """
    import requests
    from core.secrets import get_credential

    token = get_credential("BTC_LEDGER_API_TOKEN") or ""
    payload = {
        "timestamp": _epoch(result),
        "price_usd": result["price_usd"],
        "ath_usd": result.get("ath_usd"),
        "drawdown_pct": result.get("drawdown_pct"),
        "ma_200d": result.get("ma_200d"),
        "mayer_multiple": result.get("mayer_multiple"),
        "fear_greed": result.get("fear_greed"),
        "fear_greed_classification": result.get("fear_greed_classification"),
        "source": "live",
    }
    resp = requests.post(
        f"{LEDGER_URL}/observations",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bitcoin alert gate")
    parser.add_argument("--state-file", help="Path to the latch state JSON (read only)")
    parser.add_argument("--test-fixture", help="Fixture JSON, skips the live APIs")
    parser.add_argument("--ledger", action="store_true",
                        help="Write to btc-ledger even when reading a fixture. "
                             "Without it, a fixture run records nothing.")
    args = parser.parse_args()

    try:
        result = evaluate(args.state_file, args.test_fixture)
    except Exception as exc:
        print(f"evaluator failed: {exc}", file=sys.stderr)
        return BROKEN_EXIT_CODE

    code = exit_code_for(result)
    lower_latch_if_deescalated(args.state_file, result)

    if args.test_fixture and not args.ledger:
        # Fabricated prices must never enter the ledger by accident: every row
        # there is stamped `source: live`, so one hand-run fixture is a fake
        # point on the dashboard no later reading can be told apart from.
        print(json.dumps({"tier": result["tier"], "should_alert": result["should_alert"]}))
        return code

    try:
        post_observation(result)
    except Exception as exc:
        # An alert outranks the ledger: the dashboard can miss an hour, the
        # buy signal cannot. On a quiet hour there is nothing else at stake,
        # so the failure is worth being told about.
        print(f"observation post failed: {exc}", file=sys.stderr)
        if code == QUIET_EXIT_CODE:
            return BROKEN_EXIT_CODE

    print(json.dumps({"tier": result["tier"], "should_alert": result["should_alert"]}))
    return code


if __name__ == "__main__":
    sys.exit(main())
