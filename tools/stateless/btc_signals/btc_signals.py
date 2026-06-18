#!/usr/bin/env python3
"""Bitcoin buy-signal evaluator.

Pulls BTC price history from CoinGecko and the Fear & Greed Index from
alternative.me, computes accumulation signals (Mayer Multiple, drawdown
from 52-week ATH, F&G), decides a tier, recommends a buy size, and
optionally consults/updates a JSON state file to debounce repeated
alerts.

Stateless except for the optional latch state file. JSON output, no
side effects unless ``--state-file`` is given AND ``--update-state``
is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


TIER_ORDER = ["none", "opportunistic", "strong", "deep_value", "generational"]

# Buy size mapping in USD. Daniel's baseline is $150 biweekly.
BUY_SIZE_USD = {
    "none": 0,
    "opportunistic": 150,
    "strong": 300,
    "deep_value": 500,
    "generational": 750,
}

# Mayer Multiple thresholds (price / 200-day SMA).
MAYER_OPPORTUNISTIC = 1.0
MAYER_STRONG = 0.85
MAYER_DEEP_VALUE = 0.75
MAYER_GENERATIONAL = 0.65

# Drawdown thresholds (positive = larger drawdown).
DRAWDOWN_OPPORTUNISTIC = 0.25
DRAWDOWN_STRONG = 0.35
DRAWDOWN_DEEP_VALUE = 0.45
DRAWDOWN_GENERATIONAL = 0.50

# Fear & Greed thresholds (lower = more fear = more bullish for accumulation).
FG_OPPORTUNISTIC = 25
FG_STRONG = 20
FG_DEEP_VALUE = 18
FG_GENERATIONAL = 15

# Quiet hours in America/Chicago. Daniel approved 06:00 to 23:00.
QUIET_HOURS_START = 23  # 11pm — alerts suppressed at and after this hour
QUIET_HOURS_END = 6     # 6am — alerts resume at this hour


def fetch_btc_history(test_data: dict | None = None) -> dict:
    """Pull 365 days of daily BTC price from CoinGecko. Returns dict with
    `prices` (list of [ts_ms, price]) and `current` (float).
    """
    if test_data is not None:
        return test_data

    import requests
    from core.secrets import get_credential

    api_key = get_credential("COINGECKO_API_KEY")
    headers: dict[str, str] = {}
    if api_key:
        headers["x-cg-pro-api-key"] = api_key

    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    params = {"vs_currency": "usd", "days": "365", "interval": "daily"}
    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()
    prices = data.get("prices", [])
    if not prices:
        raise RuntimeError("CoinGecko returned no price data")
    return {"prices": prices, "current": prices[-1][1]}


def fetch_fear_greed(test_value: int | None = None) -> dict:
    """Pull current Fear & Greed Index from alternative.me. No key needed."""
    if test_value is not None:
        return {"value": test_value, "classification": "Test"}

    import requests

    response = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
    response.raise_for_status()
    payload = response.json()
    point = payload["data"][0]
    return {
        "value": int(point["value"]),
        "classification": point.get("value_classification", "Unknown"),
    }


def compute_signals(price_history: dict, fear_greed: dict) -> dict:
    """Compute the four signals from raw inputs. Pure function."""
    prices = [p[1] for p in price_history["prices"]]
    current = price_history["current"]
    ath = max(prices)
    drawdown = 1.0 - (current / ath)  # positive number = how far below ATH

    last_200 = prices[-200:] if len(prices) >= 200 else prices
    ma_200d = statistics.mean(last_200)
    mayer = current / ma_200d

    return {
        "price_usd": current,
        "ath_usd": ath,
        "drawdown_pct": drawdown * 100,
        "ma_200d": ma_200d,
        "mayer_multiple": mayer,
        "fear_greed": fear_greed["value"],
        "fear_greed_classification": fear_greed["classification"],
    }


def signals_active(s: dict) -> list[str]:
    """Return the list of primary signals currently firing (opportunistic level)."""
    active = []
    if s["mayer_multiple"] < MAYER_OPPORTUNISTIC:
        active.append("mayer")
    if s["drawdown_pct"] / 100 > DRAWDOWN_OPPORTUNISTIC:
        active.append("drawdown")
    if s["fear_greed"] < FG_OPPORTUNISTIC:
        active.append("fear_greed")
    return active


def decide_tier(s: dict) -> str:
    """Determine the alert tier from current signal values.

    Generational requires the deepest reading on all three signals.
    Deep value triggers on any single deep-value-grade reading or 3 of 3 stacked.
    Strong triggers on any single strong-grade reading or 2 of 3 stacked.
    Opportunistic triggers on any single opportunistic-grade reading.
    """
    mayer = s["mayer_multiple"]
    dd = s["drawdown_pct"] / 100
    fg = s["fear_greed"]
    active = signals_active(s)
    count = len(active)

    # Generational: all three at their deepest readings simultaneously.
    if mayer < MAYER_GENERATIONAL and dd > DRAWDOWN_GENERATIONAL and fg < FG_GENERATIONAL:
        return "generational"

    # Deep value: any one single deep-value reading, or all three stacked.
    if mayer < MAYER_DEEP_VALUE or dd > DRAWDOWN_DEEP_VALUE or fg < FG_DEEP_VALUE:
        return "deep_value"
    if count >= 3:
        return "deep_value"

    # Strong: any one strong-grade reading, or two stacked.
    if mayer < MAYER_STRONG or dd > DRAWDOWN_STRONG or fg < FG_STRONG:
        return "strong"
    if count >= 2:
        return "strong"

    # Opportunistic: any single signal firing at the loosest threshold.
    if count >= 1:
        return "opportunistic"

    return "none"


def tier_rank(tier: str) -> int:
    return TIER_ORDER.index(tier)


def in_quiet_hours(now: datetime | None = None) -> bool:
    """True if current time in America/Chicago is in quiet hours.

    Quiet hours run from 23:00 through 05:59. The configured "active" window
    is 06:00 through 22:59.
    """
    if now is None:
        now = datetime.now(ZoneInfo("America/Chicago"))
    else:
        now = now.astimezone(ZoneInfo("America/Chicago"))
    h = now.hour
    return h >= QUIET_HOURS_START or h < QUIET_HOURS_END


def read_state(path: Path) -> dict:
    if not path.exists():
        return {"last_tier": "none", "last_alert_at": 0}
    try:
        with path.open() as f:
            data = json.load(f)
        data.setdefault("last_tier", "none")
        data.setdefault("last_alert_at", 0)
        return data
    except Exception:
        return {"last_tier": "none", "last_alert_at": 0}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(state, f, indent=2)


def decide_alert(
    current_tier: str,
    last_tier: str,
    quiet: bool,
) -> tuple[bool, str]:
    """Return (should_alert, reason) given current tier, latched last tier,
    and whether quiet hours are active.

    Rules:
    - During quiet hours: never alert. State is NOT updated to last_tier
      yet, so escalations buffer until the active window resumes.
    - Outside quiet hours: alert only when current_tier strictly exceeds
      last_tier. De-escalation back to "none" silently resets the latch.
    """
    if quiet:
        return False, f"quiet_hours_suppressed (current={current_tier}, last={last_tier})"

    cur = tier_rank(current_tier)
    last = tier_rank(last_tier)

    if current_tier == "none":
        return False, "no signals active"
    if cur > last:
        return True, f"escalation from {last_tier} to {current_tier}"
    if cur == last:
        return False, f"already alerted at {current_tier}"
    return False, f"de-escalation from {last_tier} to {current_tier}"


def build_result(
    signals: dict,
    tier: str,
    active: list[str],
    should_alert: bool,
    reason: str,
    quiet: bool,
    previous_tier: str,
) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "price_usd": round(signals["price_usd"], 2),
        "ath_usd": round(signals["ath_usd"], 2),
        "drawdown_pct": round(signals["drawdown_pct"], 2),
        "ma_200d": round(signals["ma_200d"], 2),
        "mayer_multiple": round(signals["mayer_multiple"], 4),
        "fear_greed": signals["fear_greed"],
        "fear_greed_classification": signals["fear_greed_classification"],
        "signals_active": active,
        "tier": tier,
        "previous_tier": previous_tier,
        "suggested_buy_usd": BUY_SIZE_USD[tier],
        "should_alert": should_alert,
        "alert_reason": reason,
        "quiet_hours_active": quiet,
    }


def _load_test_data(path: str) -> tuple[dict, int]:
    """Load test fixture: {"prices": [[ts, price], ...], "fear_greed": int}."""
    with open(path) as f:
        data = json.load(f)
    return {"prices": data["prices"], "current": data["prices"][-1][1]}, int(data["fear_greed"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Bitcoin buy-signal evaluator")
    parser.add_argument("--state-file", help="Path to latch state JSON")
    parser.add_argument("--update-state", action="store_true",
                        help="Persist new state when an alert is decided")
    parser.add_argument("--test-fixture", help="Path to test fixture JSON (skips API)")
    parser.add_argument("--ignore-quiet-hours", action="store_true",
                        help="Bypass quiet hours check (useful for ad-hoc /btc checks)")
    args = parser.parse_args()

    try:
        if args.test_fixture:
            history, fg_value = _load_test_data(args.test_fixture)
            fear_greed = {"value": fg_value, "classification": "Test"}
        else:
            history = fetch_btc_history()
            fear_greed = fetch_fear_greed()

        signals = compute_signals(history, fear_greed)
        active = signals_active(signals)
        tier = decide_tier(signals)
        quiet = False if args.ignore_quiet_hours else in_quiet_hours()

        state_path = Path(args.state_file) if args.state_file else None
        previous_tier = "none"
        if state_path:
            state = read_state(state_path)
            previous_tier = state["last_tier"]

        should_alert, reason = decide_alert(tier, previous_tier, quiet)

        result = build_result(
            signals, tier, active, should_alert, reason, quiet, previous_tier
        )

        if state_path and args.update_state:
            # Only update the latch when we actually fired (or when tier is none).
            # Quiet-hour suppression must NOT update the latch — otherwise the
            # alert is lost when active hours resume.
            if should_alert or tier == "none":
                new_state = {
                    "last_tier": tier,
                    "last_alert_at": int(datetime.now(timezone.utc).timestamp())
                    if should_alert
                    else read_state(state_path).get("last_alert_at", 0),
                }
                write_state(state_path, new_state)

        print(json.dumps(result, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
