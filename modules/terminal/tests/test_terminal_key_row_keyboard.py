"""The key row does not raise the phone's keyboard.

A phone paints its keyboard over the page and re-raises it, on any later tap
anywhere, whenever an editable element holds focus. The tile's hidden xterm
textarea is such an element, so once anything has focused it every key-row tap
pops a keyboard over the thing being read — PgUp included, which on the normal
buffer only scrolls xterm's own viewport and sends nothing at all.

Two halves, because there are two ways that focus arrives:

* the tile focusing its own pane, which nobody asked for, and
* the user's own tap on the pane, which is a real request and stays.

The first is withheld on a device with a coarse pointer. The second is undone
at the key-row tap, where the pane's cursor buys nothing: `sendBytes` writes
the socket and `scrollPage` writes the socket or scrolls xterm directly.
Neither reads focus.

Headless Chromium has no IME, so what is asserted here is the focused-editable
state that decides whether a phone raises one. That the keyboard itself stays
down is verified by hand on a real phone.

The gate is the pointer and never the width. `composeBoxWanted`'s narrow arm is
true for a mouse at 800px — and for a 1280px window at 175% zoom, which shrinks
the CSS viewport the same way — so gating focus on it would leave an ordinary
desktop window silently refusing every keystroke. That window is the last test
here, and it was already broken before any of this: `focus()` on an element in
a `display: none` subtree is a no-op, and the stage was still hidden when
`addPanel` called it.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

import auth  # noqa: E402

BOX = ".term-compose-input"
TOOLBAR = "#term-mobile-toolbar"
INTERRUPT_KEY = TOOLBAR + " button:nth-child(3)"
PGUP_KEY = TOOLBAR + ' button[data-scroll="-1"]'

SESSIONS = [
    {
        "id": "sess-a",
        "mind_id": "mind-uuid",
        "mind_name": "skippy",
        "status": "running",
        "short_id": "sess-a",
    },
    {
        "id": "sess-b",
        "mind_id": "mind-uuid-2",
        "mind_name": "mordecai",
        "status": "running",
        "short_id": "sess-b",
    },
]


def _stub_page(page):
    page.route(
        "**/api/terminal/sessions*",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(SESSIONS)
        ),
    )
    page.route(
        "**/api/terminal/labels*",
        lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
    )


def _wire_sockets(page, greeting=b"ready\r\n", routes=None):
    """Mock every attach socket; hand back what each one received, per session."""
    sent = {}

    def handle_ws(ws):
        key = ws.url.split("/attach/")[1].split("?")[0]
        sent.setdefault(key, [])
        if routes is not None:
            routes.append(ws)
        ws.on_message(
            lambda m: sent[key].append(m if isinstance(m, str) else m.decode("utf8", "replace"))
        )
        ws.send(greeting)

    page.route_web_socket("**/api/terminal/attach/**", handle_ws)
    return sent


def _typed(sent, key="sess-a"):
    """Keystroke bytes only — the socket also carries resize control frames."""
    out = []
    for frame in sent.get(key, []):
        try:
            if isinstance(json.loads(frame), dict):
                continue
        except (ValueError, TypeError):
            pass
        out.append(frame)
    return "".join(out)


def _open_tile(page, base, index=0, greeting=b"ready\r\n", routes=None):
    sent = _wire_sockets(page, greeting=greeting, routes=routes)
    page.goto(base + "/terminal")
    page.wait_for_selector(".term-card", timeout=15000)
    page.query_selector_all(".term-card")[index].click()
    page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    page.wait_for_timeout(600)
    return sent


def _editable_focused(page):
    """Is the cursor in something a phone would raise its keyboard for?

    Asked of the element itself rather than of one known class name: focus
    landing in the compose box is also a keyboard, and naming only the pane's
    textarea would call that case clean.
    """
    return page.evaluate(
        """() => {
            const a = document.activeElement;
            if (!a) return null;
            if (a.isContentEditable) return a.tagName;
            if (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT') {
                return a.tagName + '.' + (a.className || '');
            }
            return null;
        }"""
    )


def _tap(page, selector):
    """A real finger, not a click: the synthesized-tap path is the one that
    moves focus on a phone, and page.click() does not take it."""
    box = page.locator(selector).bounding_box()
    page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(200)


def _context(live_app, viewport, *, touch):
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        ctx = browser.new_context(
            viewport=viewport, is_mobile=touch, has_touch=touch
        )
        ctx.add_cookies([{
            "name": auth.SESSION_COOKIE_NAME, "value": token,
            "domain": "127.0.0.1", "path": "/",
        }])
        page = ctx.new_page()
        _stub_page(page)
        yield page, base
        browser.close()


@pytest.fixture()
def phone(live_app):
    yield from _context(live_app, {"width": 412, "height": 780}, touch=True)


@pytest.fixture()
def tablet(live_app):
    """Coarse pointer, and wide enough that the stage is never hidden — which
    is the only geometry where the tile's automatic focus actually lands."""
    yield from _context(live_app, {"width": 1024, "height": 768}, touch=True)


@pytest.fixture()
def narrow_desktop(live_app):
    """A mouse in a window dragged below the 859px breakpoint. The compose box
    appears here; the keyboard question has nothing to do with it."""
    yield from _context(live_app, {"width": 800, "height": 700}, touch=False)


def test_a_touch_device_opens_a_tile_with_nothing_holding_the_cursor(tablet):
    page, base = tablet
    _open_tile(page, base)
    assert _editable_focused(page) is None


def test_a_touch_device_still_sends_the_key_row_to_the_tile_it_just_opened(tablet):
    """The ledger entry and the cursor are two decisions.

    `focusedPanel` is what the key row routes to and what paints a header, so
    withholding it along with the cursor would send Ctrl+C to whichever tile is
    first in the Map — with no tile painted as focused to say where it went.
    """
    page, base = tablet
    sent = _open_tile(page, base, index=0)
    page.query_selector_all(".term-card")[1].click()
    page.wait_for_timeout(600)
    sent.setdefault("sess-b", [])

    page.click(INTERRUPT_KEY)
    page.wait_for_timeout(300)

    assert "\x03" in _typed(sent, "sess-b"), "the interrupt missed the open tile"
    assert "\x03" not in _typed(sent, "sess-a"), "it went to the wrong terminal"


def test_an_interrupt_after_a_pane_tap_leaves_nothing_holding_the_cursor(phone):
    page, base = phone
    sent = _open_tile(page, base)

    _tap(page, ".term-panel .xterm-screen")
    assert _editable_focused(page) is not None, "the pane tap never took the cursor"

    page.click(INTERRUPT_KEY)
    page.wait_for_timeout(300)

    assert _editable_focused(page) is None
    assert "\x03" in _typed(sent), "the interrupt never reached the pty"


def test_paging_after_a_pane_tap_leaves_nothing_holding_the_cursor(phone):
    page, base = phone
    lines = "".join("line %03d\r\n" % i for i in range(200))
    _open_tile(page, base, greeting=lines.encode())

    _tap(page, ".term-panel .xterm-screen")
    assert _editable_focused(page) is not None, "the pane tap never took the cursor"

    before = page.evaluate("() => document.querySelector('.xterm-viewport').scrollTop")
    page.click(PGUP_KEY)
    page.wait_for_timeout(300)

    assert _editable_focused(page) is None
    after = page.evaluate("() => document.querySelector('.xterm-viewport').scrollTop")
    assert after < before, "the view did not page"


def test_dismissing_the_rotation_banner_does_not_take_the_cursor(phone):
    """The one automatic focus that does land at phone width.

    A tile below the breakpoint focuses nothing on open — the stage is still
    hidden at that moment, so the call is a no-op. By the time a rotation
    banner can be dismissed the stage is up, so this focus lands, and
    dismissing a banner is not a request to type.
    """
    page, base = phone
    routes = []
    _open_tile(page, base, routes=routes)
    assert _editable_focused(page) is None

    routes[0].send(json.dumps({
        "type": "session_rotated",
        "exchange": [["you", "where were we"], ["skippy", "right here"]],
    }))
    page.wait_for_selector(".term-rotated:not([hidden])", timeout=5000)
    page.click(".term-rotated-close")
    page.wait_for_timeout(250)

    assert _editable_focused(page) is None


def test_a_narrow_desktop_window_types_straight_into_the_terminal(narrow_desktop):
    """A mouse below the breakpoint still gets the cursor. The compose box
    appears at this width, which is why its gate is the wrong one to reuse."""
    page, base = narrow_desktop
    sent = _open_tile(page, base)

    page.keyboard.type("hello")
    page.wait_for_timeout(300)

    assert "hello" in _typed(sent), "a narrow desktop window accepted no typing"
