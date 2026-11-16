"""The Chief of Staff barrel's time window, driven in a real browser
(feature 6.11).

What is asserted is what Daniel sees: which finished work the barrel lists,
read from the page the app serves, before and after he chooses a window and
comes back.
"""

import time

from browser_page import ORIGIN, browser, signed_in_client
from hive_data import staff
from hive_site.testing import run

PAGE = ORIGIN + "/chief-of-staff"
DAY = 24 * 60 * 60

_LISTED = """() => Array.from(
  document.querySelectorAll('#cos-barrel .cos-done span:last-child'),
  n => n.textContent)"""


async def _finished(name, days_ago):
    moment = time.time() - days_ago * DAY
    await staff.create_task(name, "done", now=moment)


def _listed_once_loaded(page, expected):
    page.wait_for_function(
        "(want) => document.querySelectorAll('#cos-barrel .cos-done').length"
        " === want", arg=expected)
    return page.evaluate(_LISTED)


def test_a_chosen_window_is_still_in_force_on_the_next_visit(monkeypatch):
    run(staff.add_status("done"))
    run(_finished("Finished yesterday", 1))
    run(_finished("Finished three weeks ago", 21))
    client = signed_in_client(monkeypatch)
    with browser(client) as context:
        ctx = context(1400, 900)
        page = ctx.new_page()
        page.goto(PAGE)
        # Nothing chosen yet: seven days, so three weeks ago is left out.
        assert _listed_once_loaded(page, 1) == ["Finished yesterday"]

        page.select_option("#cos-window", "30")
        assert _listed_once_loaded(page, 2) == [
            "Finished yesterday", "Finished three weeks ago"]
        page.close()

        # The next visit, in the same browser.
        again = ctx.new_page()
        again.goto(PAGE)
        assert _listed_once_loaded(again, 2) == [
            "Finished yesterday", "Finished three weeks ago"]
        assert again.input_value("#cos-window") == "30"
