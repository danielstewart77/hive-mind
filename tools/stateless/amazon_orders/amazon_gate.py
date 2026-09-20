"""The morning check that decides whether Ada is woken about Amazon spending.

Runs on the scheduler's cron as a plain subprocess: no session, no mind, no
tool calls. It asks Gmail whether any Amazon *order confirmation* arrived
yesterday. Nothing bought, nothing said — no session, no post, no voice note.

Only confirmations count. Amazon also sends shipping notices, delivery
notices, review requests and account nags, and every one of them would wake
the mind to report a purchase that already went in the ledger days ago.
A confirmation comes from `auto-confirm@amazon.com` and is the only mail
carrying an order total, which is why it is the ledger's source of truth.
The sender is the filter, because Amazon has used at least two subject
formats for the same mail — today's "Ordered 2 items: ..." and the older
"Your Amazon.com order of ..." — and pinning the current one drops a real
purchase the day Amazon changes it back. What the subject is used for is the
opposite: throwing out the cancellations and refunds that share the address.

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
# Mail from the confirmation address that is not a purchase.
NOT_A_PURCHASE = ("cancel", "refund", "return", "delayed", "problem with your order")
# Amazon wraps item counts in bidi isolates: "Ordered: \u20661\u2069 Apparel item".
BIDI_MARKS = "\u2066\u2067\u2068\u2069\u200e\u200f"
TIMEZONE = ZoneInfo(os.environ.get("SPENDING_TIMEZONE", "America/Chicago"))
HIVE_TOOLS_URL = os.environ.get("HIVE_TOOLS_URL", "http://hive-tools:9421")


def as_central(now: datetime) -> datetime:
    """Read a timestamp in Daniel's own timezone.

    A naive timestamp is *assumed* Central rather than converted from it: the
    scheduler container runs on UTC, so letting the system decide shifts the
    window by a day for the first six hours of every Central day.
    """
    if now.tzinfo is None:
        return now.replace(tzinfo=TIMEZONE)
    return now.astimezone(TIMEZONE)


def yesterday_window(now: datetime) -> tuple[datetime, datetime]:
    """The one day the gate asks about: yesterday, midnight to midnight, in
    Daniel's own timezone.

    Returned as instants rather than dates because Gmail resolves a bare
    `after:2026/09/19` in the *account's* timezone, not the one computed
    here. On this account that is UTC, so every order placed after seven in
    the evening falls into the next day's bare-date window — it is reported a
    morning late and, worse, stamped with the wrong date. Two thirds of these
    orders are placed in the evening.
    """
    today = as_central(now).date()
    start = datetime(today.year, today.month, today.day, tzinfo=TIMEZONE)
    return start - timedelta(days=1), start


def gmail_query(after: datetime, before: datetime) -> str:
    """Gmail accepts epoch seconds for `after`/`before`, which name an instant
    rather than a day and so carry no timezone ambiguity at all."""
    return (
        f"from:{CONFIRMATION_SENDER} "
        f"after:{int(after.timestamp())} before:{int(before.timestamp())}"
    )


def is_order_confirmation(message: dict) -> bool:
    """Whether one message is an order confirmation.

    The sender decides. The subject only excludes: a cancellation or refund
    notice from the same address is not a purchase, while an unfamiliar
    subject from that address almost certainly is — Amazon has shipped this
    mail under more than one wording, and a gate that recognises only the
    current one goes quiet on the day they change it, which reads exactly
    like a day nothing was bought.
    """
    sender = (message.get("from") or "").lower()
    if CONFIRMATION_SENDER not in sender:
        return False
    subject = (message.get("subject") or "")
    for mark in BIDI_MARKS:
        subject = subject.replace(mark, "")
    subject = subject.strip().lower()
    return not any(phrase in subject for phrase in NOT_A_PURCHASE)


def _fetch(url: str, token: str, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"hive-tools returned HTTP {resp.status}")
        return json.loads(resp.read().decode())


def confirmations_for(now: datetime, token: str, fetch=None) -> list[dict]:
    """Yesterday's order confirmations.

    `fetch` resolves at call time rather than being bound as a default, so
    replacing the module's fetcher actually replaces the one used — a default
    argument is bound once at import and would quietly keep issuing real
    requests.
    """
    fetch = fetch or _fetch
    after, before = yesterday_window(now)
    query = urllib.parse.quote(gmail_query(after, before))
    url = f"{HIVE_TOOLS_URL}/gmail/messages?query={query}&max_results=50"
    payload = fetch(url, token)
    # hive-tools reports a Gmail-side failure — an expired OAuth token, a rate
    # limit, a revoked scope — as HTTP 200 with an `error` body and no
    # `messages` key. Read naively that is an empty inbox, which is the one
    # answer this gate must never give by accident.
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"gmail error: {payload['error']}")
    if not isinstance(payload, dict) or "messages" not in payload:
        raise RuntimeError(f"unrecognised mail response: {str(payload)[:200]}")
    return [m for m in payload["messages"] if is_order_confirmation(m)]


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
