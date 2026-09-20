"""Tests for the Amazon order gate — the morning check that decides whether
Ada is woken about spending at all.

Each test names the requirement it protects.
"""

import importlib.util
import sys
import urllib.error
from datetime import date, datetime
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
    def _fetch(url, token, timeout=30.0):
        _fetch.url = url
        return {"messages": messages, "returned": len(messages)}
    return _fetch


# ---------------------------------------------------------------------------
# R1/R2 — a confirmation wakes her, nothing does not
# ---------------------------------------------------------------------------
def test_a_confirmation_from_yesterday_fires_the_task(gate):
    now = datetime(2026, 9, 20, 7, 15, tzinfo=CENTRAL)

    found = gate.confirmations_for(now, "tok", _fetcher([_confirmation()]))

    assert gate.exit_code_for(found) == 10


def test_a_morning_with_no_amazon_mail_stays_silent(gate):
    now = datetime(2026, 9, 20, 7, 15, tzinfo=CENTRAL)

    found = gate.confirmations_for(now, "tok", _fetcher([]))

    assert gate.exit_code_for(found) == 0


# ---------------------------------------------------------------------------
# R3 — the window is yesterday, and only yesterday
# ---------------------------------------------------------------------------
def test_the_search_window_is_the_previous_day(gate):
    """Asking about today misses every evening order; asking wider re-reports
    purchases already in the ledger."""
    now = datetime(2026, 9, 20, 7, 15, tzinfo=CENTRAL)

    after, before = gate.yesterday_window(now)

    assert after == date(2026, 9, 19)
    assert before == date(2026, 9, 20)


def test_the_window_rolls_back_across_a_month_boundary(gate):
    now = datetime(2026, 10, 1, 7, 15, tzinfo=CENTRAL)

    after, before = gate.yesterday_window(now)

    assert after == date(2026, 9, 30)
    assert before == date(2026, 10, 1)


def test_the_window_is_read_in_central_time_not_utc(gate):
    """At 1am Central on the 20th it is already the 20th in UTC. A gate on UTC
    would ask about the 19th's mail on the morning of the 21st."""
    just_after_midnight_central = datetime(2026, 9, 20, 1, 0, tzinfo=CENTRAL)

    after, _ = gate.yesterday_window(just_after_midnight_central)

    assert after == date(2026, 9, 19)


def test_the_window_reaches_the_gmail_query(gate):
    """A window computed and not sent protects nothing."""
    now = datetime(2026, 9, 20, 7, 15, tzinfo=CENTRAL)
    fetch = _fetcher([])

    gate.confirmations_for(now, "tok", fetch)

    assert "after%3A2026/09/19" in fetch.url
    assert "before%3A2026/09/20" in fetch.url


# ---------------------------------------------------------------------------
# R4 — only confirmations count
# ---------------------------------------------------------------------------
def test_a_shipping_notice_does_not_wake_her(gate):
    """A shipment is not a purchase; that money was recorded days ago."""
    now = datetime(2026, 9, 20, 7, 15, tzinfo=CENTRAL)
    shipped = {
        "from": '"Amazon.com" <shipment-tracking@amazon.com>',
        "subject": "Shipped: Ordered 2 items",
    }

    found = gate.confirmations_for(now, "tok", _fetcher([shipped]))

    assert gate.exit_code_for(found) == 0


def test_an_account_notice_from_amazon_does_not_wake_her(gate):
    now = datetime(2026, 9, 20, 7, 15, tzinfo=CENTRAL)
    nag = {
        "from": "Amazon <noreply@amazon.com>",
        "subject": "Action Required to Maintain your Inactive Amazon Child Profile",
    }

    found = gate.confirmations_for(now, "tok", _fetcher([nag]))

    assert gate.exit_code_for(found) == 0


def test_a_cancellation_from_the_confirmation_address_does_not_wake_her(gate):
    """Same sender, not a purchase."""
    now = datetime(2026, 9, 20, 7, 15, tzinfo=CENTRAL)
    cancelled = {
        "from": '"Amazon.com" <auto-confirm@amazon.com>',
        "subject": "Your order has been canceled",
    }

    found = gate.confirmations_for(now, "tok", _fetcher([cancelled]))

    assert gate.exit_code_for(found) == 0


def test_a_confirmation_among_the_noise_still_wakes_her(gate):
    now = datetime(2026, 9, 20, 7, 15, tzinfo=CENTRAL)
    messages = [
        {"from": "Amazon <noreply@amazon.com>", "subject": "Rate your recent purchase"},
        _confirmation("Ordered: 1 Electronics item"),
        {"from": '"Amazon.com" <shipment-tracking@amazon.com>', "subject": "Delivered"},
    ]

    found = gate.confirmations_for(now, "tok", _fetcher(messages))

    assert len(found) == 1
    assert gate.exit_code_for(found) == 10


# ---------------------------------------------------------------------------
# R5 — a gate that cannot look says so
# ---------------------------------------------------------------------------
def test_unreachable_mail_is_reported_broken_rather_than_quiet(gate, monkeypatch):
    def _refuse(url, token, timeout=30.0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(gate, "_fetch", _refuse)
    monkeypatch.setattr(sys, "argv", ["amazon_gate.py", "--now", "2026-09-20T07:15:00-05:00"])
    monkeypatch.setenv("HIVE_TOOLS_TOKEN", "tok")

    assert gate.main() == 1


def test_a_refused_token_is_reported_broken_rather_than_quiet(gate, monkeypatch):
    def _refuse(url, token, timeout=30.0):
        raise RuntimeError("hive-tools returned HTTP 401")

    monkeypatch.setattr(gate, "_fetch", _refuse)
    monkeypatch.setattr(sys, "argv", ["amazon_gate.py", "--now", "2026-09-20T07:15:00-05:00"])
    monkeypatch.setenv("HIVE_TOOLS_TOKEN", "tok")

    assert gate.main() == 1


def test_a_missing_token_is_reported_broken_rather_than_quiet(gate, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["amazon_gate.py", "--now", "2026-09-20T07:15:00-05:00"])
    monkeypatch.setenv("HIVE_TOOLS_TOKEN", "")
    monkeypatch.setitem(
        sys.modules, "keyring",
        type("K", (), {"get_password": staticmethod(lambda *a: None)}),
    )

    assert gate.main() == 1
