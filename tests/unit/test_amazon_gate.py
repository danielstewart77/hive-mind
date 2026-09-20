"""Tests for the Amazon order gate — the morning check that decides whether
Ada is woken about spending at all.

Each test names the requirement it protects.
"""

import importlib.util
import io
import json
import sys
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

GATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools" / "stateless" / "amazon_orders" / "amazon_gate.py"
)
CENTRAL = ZoneInfo("America/Chicago")


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("amazon_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["amazon_gate"] = module
    spec.loader.exec_module(module)
    return module


def _confirmation(subject="Ordered 2 items: Supplements, Coffee"):
    return {"from": '"Amazon.com" <auto-confirm@amazon.com>', "subject": subject}


def _fetcher(messages):
    """A stand-in for the network call. Records the URL it was asked for."""
    def _fetch(url, token, timeout=30.0):
        _fetch.url = url
        _fetch.token = token
        return {"messages": messages, "returned": len(messages)}
    return _fetch


def _run_main(gate, monkeypatch, fetch, *, now="2026-09-20T07:15:00-05:00", token="tok"):
    monkeypatch.setattr(gate, "_fetch", fetch)
    monkeypatch.setattr(sys, "argv", ["amazon_gate.py", "--now", now])
    monkeypatch.setenv("HIVE_TOOLS_TOKEN", token)
    return gate.main()


# ---------------------------------------------------------------------------
# R1/R2 — the exit code the scheduler actually receives
# ---------------------------------------------------------------------------
def test_a_confirmation_from_yesterday_fires_the_task(gate, monkeypatch):
    assert _run_main(gate, monkeypatch, _fetcher([_confirmation()])) == 10


def test_a_morning_with_no_amazon_mail_stays_silent(gate, monkeypatch):
    assert _run_main(gate, monkeypatch, _fetcher([])) == 0


def test_the_fetcher_the_module_holds_is_the_one_that_runs(gate, monkeypatch):
    """Bound as a default argument, a replaced fetcher is ignored and the real
    network call happens instead — which is how a failure-path test passes by
    accident against a live host."""
    fetch = _fetcher([_confirmation()])

    _run_main(gate, monkeypatch, fetch)

    assert hasattr(fetch, "url")


def test_the_token_reaches_the_call(gate, monkeypatch):
    fetch = _fetcher([])

    _run_main(gate, monkeypatch, fetch, token="fake-token-for-test")  # secret-guard: allow

    assert fetch.token == "fake-token-for-test"


# ---------------------------------------------------------------------------
# R3 — the window is yesterday, in Central, and it reaches the query
# ---------------------------------------------------------------------------
def test_the_search_window_is_the_previous_day_midnight_to_midnight(gate):
    after, before = gate.yesterday_window(datetime(2026, 9, 20, 7, 15, tzinfo=CENTRAL))

    assert after == datetime(2026, 9, 19, 0, 0, tzinfo=CENTRAL)
    assert before == datetime(2026, 9, 20, 0, 0, tzinfo=CENTRAL)


def test_the_window_rolls_back_across_a_month_boundary(gate):
    after, before = gate.yesterday_window(datetime(2026, 10, 1, 7, 15, tzinfo=CENTRAL))

    assert after == datetime(2026, 9, 30, 0, 0, tzinfo=CENTRAL)
    assert before == datetime(2026, 10, 1, 0, 0, tzinfo=CENTRAL)


def test_a_utc_timestamp_is_converted_to_central_before_the_date_is_taken(gate):
    """At 02:00 UTC it is already the 20th in UTC but still the 19th in
    Chicago, so yesterday is the 18th. A gate reading UTC dates would ask
    about the wrong day for six hours out of every twenty-four."""
    after, before = gate.yesterday_window(datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc))

    assert after.date() == date(2026, 9, 18)
    assert before.date() == date(2026, 9, 19)


def test_the_window_is_sent_as_instants_not_as_bare_dates(gate):
    """Gmail resolves a bare `after:2026/09/19` in the account's own timezone,
    which is UTC here — so an order placed at 20:32 Chicago falls outside the
    bare-date window for its own day. Epoch seconds name an instant and carry
    no timezone at all."""
    after, before = gate.yesterday_window(datetime(2026, 9, 16, 7, 15, tzinfo=CENTRAL))
    evening_order = datetime(2026, 9, 15, 20, 32, tzinfo=CENTRAL)

    query = gate.gmail_query(after, before)

    assert f"after:{int(after.timestamp())}" in query
    assert f"before:{int(before.timestamp())}" in query
    assert "2026/09/15" not in query
    # The order this window exists to catch is inside it.
    assert after <= evening_order < before


def test_a_naive_timestamp_is_assumed_central_rather_than_the_machines_zone(gate):
    """Asserted on the zone itself, not on a resulting date: this workstation
    is already Central, so a date assertion would pass here and still shift
    the window by a day inside the UTC scheduler container."""
    naive = datetime(2026, 9, 20, 2, 0)

    assert gate.as_central(naive).tzinfo is gate.TIMEZONE


def test_a_zoned_timestamp_is_converted_rather_than_relabelled(gate):
    """Relabelling 02:00 UTC as 02:00 Central moves it five hours and lands it
    on the wrong side of midnight."""
    utc_two_am = datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc)

    converted = gate.as_central(utc_two_am)

    assert converted.tzinfo is gate.TIMEZONE
    assert converted.hour == 21
    assert converted.date() == date(2026, 9, 19)


def test_the_window_and_the_sender_both_reach_the_gmail_query(gate, monkeypatch):
    """A window computed and not sent protects nothing, and a query missing
    the sender pulls every mail Daniel received yesterday."""
    fetch = _fetcher([])

    _run_main(gate, monkeypatch, fetch)

    assert "auto-confirm%40amazon.com" in fetch.url
    after = datetime(2026, 9, 19, 0, 0, tzinfo=CENTRAL)
    before = datetime(2026, 9, 20, 0, 0, tzinfo=CENTRAL)
    assert f"after%3A{int(after.timestamp())}" in fetch.url
    assert f"before%3A{int(before.timestamp())}" in fetch.url
    assert "max_results=50" in fetch.url
    assert "/gmail/messages?" in fetch.url


# ---------------------------------------------------------------------------
# R4 — only purchases count
# ---------------------------------------------------------------------------
def test_a_shipping_notice_does_not_wake_her(gate, monkeypatch):
    """A shipment is not a purchase; that money was recorded days ago."""
    shipped = {
        "from": '"Amazon.com" <shipment-tracking@amazon.com>',
        "subject": "Shipped: Ordered 2 items",
    }

    assert _run_main(gate, monkeypatch, _fetcher([shipped])) == 0


def test_an_account_notice_from_amazon_does_not_wake_her(gate, monkeypatch):
    nag = {
        "from": "Amazon <noreply@amazon.com>",
        "subject": "Ordered profiles: Action Required to Maintain your Child Profile",
    }

    assert _run_main(gate, monkeypatch, _fetcher([nag])) == 0


def test_a_cancellation_from_the_confirmation_address_does_not_wake_her(gate, monkeypatch):
    cancelled = _confirmation("Your order has been canceled")

    assert _run_main(gate, monkeypatch, _fetcher([cancelled])) == 0


def test_a_refund_notice_does_not_wake_her(gate, monkeypatch):
    refund = _confirmation("Your refund for order 112-3879109-7165824")

    assert _run_main(gate, monkeypatch, _fetcher([refund])) == 0


def test_the_older_confirmation_wording_still_wakes_her(gate, monkeypatch):
    """Amazon has shipped this mail as "Your Amazon.com order of ..." as well
    as "Ordered ...". A gate pinned to today's wording goes quiet the day they
    change it, and a quiet morning is indistinguishable from an honest one."""
    older = _confirmation('Your Amazon.com order of "Dr. STONE, Vol. 6" and 6 more items.')

    assert _run_main(gate, monkeypatch, _fetcher([older])) == 10


def test_a_bidi_wrapped_subject_still_wakes_her(gate, monkeypatch):
    """Amazon wraps item counts in directional isolates."""
    wrapped = _confirmation("Ordered: ⁦1⁩ Nutrition & Wellness item")

    assert _run_main(gate, monkeypatch, _fetcher([wrapped])) == 10


def test_a_confirmation_among_the_noise_still_wakes_her(gate, monkeypatch):
    messages = [
        {"from": "Amazon <noreply@amazon.com>", "subject": "Rate your recent purchase"},
        _confirmation("Ordered: 1 Electronics item"),
        {"from": '"Amazon.com" <shipment-tracking@amazon.com>', "subject": "Delivered"},
    ]

    assert _run_main(gate, monkeypatch, _fetcher(messages)) == 10


# ---------------------------------------------------------------------------
# R5 — a gate that cannot look says so
# ---------------------------------------------------------------------------
def test_a_gmail_error_reported_as_success_is_not_read_as_an_empty_inbox(gate, monkeypatch):
    """hive-tools answers a failed Gmail call with HTTP 200 and an `error`
    body. An expired OAuth token is the likeliest failure on this path, and
    read naively it is a morning that bought nothing."""
    def _auth_expired(url, token, timeout=30.0):
        return {"error": "Gmail not authenticated"}

    assert _run_main(gate, monkeypatch, _auth_expired) == 1


def test_a_response_with_no_message_list_is_not_read_as_an_empty_inbox(gate, monkeypatch):
    def _garbage(url, token, timeout=30.0):
        return {"detail": "Not Found"}

    assert _run_main(gate, monkeypatch, _garbage) == 1


def test_unreachable_mail_is_reported_broken_rather_than_quiet(gate, monkeypatch):
    def _refuse(url, token, timeout=30.0):
        raise urllib.error.URLError("connection refused")

    assert _run_main(gate, monkeypatch, _refuse) == 1


def test_a_refused_token_is_reported_broken_rather_than_quiet(gate, monkeypatch):
    def _refuse(url, token, timeout=30.0):
        raise RuntimeError("hive-tools returned HTTP 401")

    assert _run_main(gate, monkeypatch, _refuse) == 1


def test_a_missing_token_is_reported_broken_rather_than_quiet(gate, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["amazon_gate.py", "--now", "2026-09-20T07:15:00-05:00"])
    monkeypatch.setenv("HIVE_TOOLS_TOKEN", "")
    monkeypatch.setitem(
        sys.modules, "keyring",
        type("K", (), {"get_password": staticmethod(lambda *a: None)}),
    )
    monkeypatch.setattr(gate, "_fetch", _fetcher([_confirmation()]))

    assert gate.main() == 1


# ---------------------------------------------------------------------------
# R5 — the fetcher itself
# ---------------------------------------------------------------------------
def test_the_request_carries_the_bearer_credential(gate, monkeypatch):
    """hive-tools answers 401 without it, and this gate is the only caller."""
    seen = {}

    class _Resp:
        status = 200

        def read(self):
            return json.dumps({"messages": []}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(gate.urllib.request, "urlopen", _urlopen)

    gate._fetch("http://hive-tools:9421/gmail/messages", "tok-123")

    assert seen["auth"] == "Bearer tok-123"


def test_a_non_200_from_hive_tools_is_raised_rather_than_parsed(gate, monkeypatch):
    class _Resp:
        status = 503

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(gate.urllib.request, "urlopen", lambda request, timeout=None: _Resp())

    with pytest.raises(RuntimeError):
        gate._fetch("http://hive-tools:9421/gmail/messages", "tok")
