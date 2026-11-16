"""The Chief of Staff page's done bucket, driven in a real browser (feature 3.6).

What is asserted is what Daniel sees: whether the bucket is on screen, and
where its edges are against the window — read from the page the app serves.
"""

import pytest

from browser_page import ORIGIN, browser, signed_in_client

PAGE = ORIGIN + "/chief-of-staff"

# Where the bucket is, and whether it can be seen there.
_BUCKET = """() => {
  const pane = document.getElementById('cos-barrel-pane');
  const box = pane.getBoundingClientRect();
  const style = getComputedStyle(pane);
  return {left: box.left, right: box.right, width: box.width,
          window: document.documentElement.clientWidth,
          shown: style.display !== 'none' && style.visibility !== 'hidden'};
}"""


def _settled(page):
    # The bucket slides over .2s; read it once it has arrived.
    page.wait_for_timeout(350)
    return page.evaluate(_BUCKET)


def _open_on_screen(bucket):
    return (bucket["shown"] and bucket["width"] > 0
            and bucket["left"] >= 0 and bucket["right"] <= bucket["window"])


def _docked(bucket):
    return not bucket["shown"] or bucket["left"] >= bucket["window"]


@pytest.mark.parametrize("width", [390, 700], ids=["narrow-phone", "unfolded-phone"])
def test_the_done_button_opens_a_docked_bucket_and_the_close_control_docks_it(
        monkeypatch, width):
    client = signed_in_client(monkeypatch)
    with browser(client) as context:
        page = context(width, 800, touch=True).new_page()
        page.goto(PAGE)
        page.wait_for_load_state("networkidle")
        assert _docked(_settled(page))

        page.locator("[data-barrel-toggle]:visible").tap()
        assert _open_on_screen(_settled(page))

        page.locator("[data-barrel-close]").tap()
        assert _docked(_settled(page))


def test_an_open_bucket_on_a_large_screen_stays_in_the_window_however_it_is_resized(
        monkeypatch):
    client = signed_in_client(monkeypatch)
    with browser(client) as context:
        page = context(1400, 900).new_page()
        page.goto(PAGE)
        page.wait_for_load_state("networkidle")
        assert _open_on_screen(_settled(page))

        for width in [2400, 900, 2200, 1000, 840, 1600, 1100]:
            page.set_viewport_size({"width": width, "height": 900})
            bucket = _settled(page)
            assert _open_on_screen(bucket), (width, bucket)

        # Docked, resized, reopened: in view again.
        page.locator("[data-barrel-close]").click()
        page.set_viewport_size({"width": 2400, "height": 900})
        _settled(page)
        page.set_viewport_size({"width": 950, "height": 900})
        page.locator("[data-barrel-toggle]:visible").click()
        bucket = _settled(page)
        assert _open_on_screen(bucket), bucket
