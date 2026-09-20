"""The morning check that decides whether Ada is woken about Amazon spending.

Runs on the scheduler's cron as a plain subprocess: no session, no mind, no
tool calls. It asks Gmail whether any Amazon *order confirmation* arrived
yesterday. Nothing bought, nothing said — no session, no post, no voice note.

Only confirmations count. Amazon also sends shipping notices, delivery
notices, review requests and account nags, and every one of them would wake
the mind to report a purchase that already went in the ledger days ago.
A confirmation comes from `auto-confirm@amazon.com` and is the only mail
carrying an order total, which is why it is the ledger's source of truth.

Exit codes: 0 quiet, 10 fire, 1 broken. A gate that cannot reach Gmail exits
broken rather than quiet — silence has to mean "you bought nothing", never
"nobody could check".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# The keyring backend this hive configures lives under the app root, so
# `import keyring` resolves its class only with that root importable. A gate
# runs from whatever directory the scheduler happens to be in, which is not
# that one.
sys.path.insert(0, "/usr/src/app")

FIRE_EXIT_CODE = 10
QUIET_EXIT_CODE = 0
BROKEN_EXIT_CODE = 1

CONFIRMATION_SENDER = "auto-confirm@amazon.com"
TIMEZONE = ZoneInfo(os.environ.get("SPENDING_TIMEZONE", "America/Chicago"))
HIVE_TOOLS_URL = os.environ.get("HIVE_TOOLS_URL", "http://hive-tools:9421")


def yesterday_window(now: datetime) -> tuple[date, date]:
    """The one day the gate asks about, as Gmail's half-open `after`/`before`.

    Gmail's `after:` is inclusive and `before:` exclusive, so yesterday is
    (yesterday, today). Asking about today would miss the evening's orders
    every single morning; asking a wider window would re-report purchases
    already in the ledger.
    """
    today = now.astimezone(TIMEZONE).date()
    return today - timedelta(days=1), today


def gmail_query(after: date, before: date) -> str:
    return (
        f"from:{CONFIRMATION_SENDER} "
        f"after:{after:%Y/%m/%d} before:{before:%Y/%m/%d}"
    )


def is_order_confirmation(message: dict) -> bool:
    """Whether one message is an order confirmation.

    The sender alone is the strong signal, but Amazon has sent cancellation
    and change notices from the same address; the subject is what separates
    "Ordered 2 items" from "Your order has shipped".
    """
    sender = (message.get("from") or "").lower()
    if CONFIRMATION_SENDER not in sender:
        return False
    subject = (message.get("subject") or "").strip().lower()
    return subject.startswith("ordered")


def _fetch(url: str, token: str, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"hive-tools returned HTTP {resp.status}")
        return json.loads(resp.read().decode())


def confirmations_for(now: datetime, token: str, fetch=_fetch) -> list[dict]:
    after, before = yesterday_window(now)
    query = urllib.parse.quote(gmail_query(after, before))
    url = f"{HIVE_TOOLS_URL}/gmail/messages?query={query}&max_results=50"
    payload = fetch(url, token)
    return [m for m in payload.get("messages", []) if is_order_confirmation(m)]


def exit_code_for(confirmations: list[dict]) -> int:
    return FIRE_EXIT_CODE if confirmations else QUIET_EXIT_CODE


def main() -> int:
    parser = argparse.ArgumentParser(description="Amazon order-confirmation gate")
    parser.add_argument("--now", help="ISO timestamp to evaluate as (testing)")
    args = parser.parse_args()

    token = os.environ.get("HIVE_TOOLS_TOKEN", "")
    if not token:
        try:
            import keyring
            token = keyring.get_password("hive-mind", "HIVE_TOOLS_TOKEN") or ""
        except Exception:
            token = ""
    if not token:
        print("no HIVE_TOOLS_TOKEN available", file=sys.stderr)
        return BROKEN_EXIT_CODE

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(TIMEZONE)

    try:
        found = confirmations_for(now, token)
    except (urllib.error.URLError, RuntimeError, ValueError, OSError) as exc:
        # Quiet means "you bought nothing". It must never mean "I could not
        # look" — that is a morning of spending silently missing from the
        # ledger with a healthy-looking green log behind it.
        print(f"could not read mail: {exc}", file=sys.stderr)
        return BROKEN_EXIT_CODE

    print(json.dumps({"confirmations": len(found)}))
    return exit_code_for(found)


if __name__ == "__main__":
    sys.exit(main())
