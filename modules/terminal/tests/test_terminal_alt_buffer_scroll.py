"""The tile's scroll controls have to reach tmux's history.

The pane is tmux, and the alternate screen the tile sees is tmux's own.
xterm has no scrollback there -- scrollLines moves nothing and the
viewport has no overflow to drag -- and the page keys are no better: tmux
hands them straight to the program, which has nothing behind it to show.
The 50,000 lines of history are tmux's, and a wheel report is the only
thing that reaches them. So both controls, the PgUp/PgDn buttons and a
finger drag, put wheel reports on the wire.

A real browser, because the thing under test is xterm's buffer state
after a real escape sequence and the bytes a real click puts on the
socket.
"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

import auth  # noqa: E402

PHONE = {"width": 412, "height": 780}
SESSIONS = [
    {
        "id": "sess-1",
        "mind_id": "mind-uuid",
        "mind_name": "skippy",
        "status": "running",
        "short_id": "sess-1",
    }
]
# What the TUI actually sends first: alternate buffer, cleared, with SGR
# mouse reporting on. Captured from a real `claude` under a pty.
ENTER_ALT_SCREEN = "\x1b[?1049h\x1b[2J\x1b[H\x1b[?1000h\x1b[?1002h\x1b[?1006h"
WHEEL_UP = re.compile(r"\x1b\[<64;\d+;\d+M")
WHEEL_DOWN = re.compile(r"\x1b\[<65;\d+;\d+M")


def _open_tile(page, base, greeting):
    """Mount one tile whose socket opens with `greeting`, recording sends."""
    sent = []

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

    def handle_ws(ws):
        ws.on_message(
            lambda m: sent.append(m if isinstance(m, str) else m.decode("utf8", "replace"))
        )
        # Terminal output is BINARY on the wire; TEXT is the
        # gateway's control channel and the tile never prints it.
        ws.send(greeting.encode())

    page.route_web_socket("**/api/terminal/attach/**", handle_ws)
    page.goto(base + "/terminal")
    page.wait_for_selector(".term-card", timeout=15000)
    page.click(".term-card")
    page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    page.wait_for_timeout(600)
    return sent


@pytest.fixture()
def phone(live_app):
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        ctx = browser.new_context(
            viewport=PHONE, device_scale_factor=2.625, is_mobile=True, has_touch=True
        )
        ctx.add_cookies(
            [
                {
                    "name": auth.SESSION_COOKIE_NAME,
                    "value": token,
                    "domain": "127.0.0.1",
                    "path": "/",
                }
            ]
        )
        yield ctx.new_page(), base
        browser.close()


def test_the_page_buttons_scroll_tmux_history(phone):
    page, base = phone
    sent = _open_tile(page, base, ENTER_ALT_SCREEN + "the tail of a long reply")
    sent.clear()

    page.click('#term-mobile-toolbar button[data-scroll="-1"]')
    page.wait_for_timeout(200)
    up = "".join(sent)
    sent.clear()
    page.click('#term-mobile-toolbar button[data-scroll="1"]')
    page.wait_for_timeout(200)
    down = "".join(sent)

    # A page is a burst of notches, not one: tmux moves a handful of lines
    # per notch, so a single one would be a nudge rather than a page.
    assert len(WHEEL_UP.findall(up)) > 1
    assert WHEEL_UP.sub("", up) == ""
    assert len(WHEEL_DOWN.findall(down)) == len(WHEEL_UP.findall(up))
    assert WHEEL_DOWN.sub("", down) == ""


def test_a_finger_drag_becomes_the_wheel_reports_the_tui_asked_for(phone):
    page, base = phone
    sent = _open_tile(page, base, ENTER_ALT_SCREEN + "the tail of a long reply")
    sent.clear()

    box = page.locator(".term-xterm").bounding_box()
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.touchscreen.tap(x, y)  # a tap is an ordinary click, never a scroll
    page.wait_for_timeout(150)
    tap = "".join(sent)
    assert "\x1b[<64;" not in tap and "\x1b[<65;" not in tap
    sent.clear()

    page.evaluate(
        """([x, y]) => {
            const host = document.querySelector('.term-xterm');
            const touch = (cx, cy) => new Touch({identifier: 1, target: host, clientX: cx, clientY: cy});
            const fire = (type, cx, cy) => host.dispatchEvent(new TouchEvent(type, {
                bubbles: true, cancelable: true,
                touches: type === 'touchend' ? [] : [touch(cx, cy)],
                changedTouches: [touch(cx, cy)],
            }));
            fire('touchstart', x, y);
            fire('touchmove', x, y + 80);   // dragging down = back in history
            fire('touchend', x, y + 80);
        }""",
        [x, y],
    )
    page.wait_for_timeout(200)

    wheel = "".join(sent)
    assert wheel.count("\x1b[<64;") == 3  # 80px at 24px a notch
    assert "\x1b[<65;" not in wheel


def test_a_tile_with_its_own_scrollback_still_scrolls_locally(phone):
    # The normal buffer is the tile's to scroll, and pushing page keys at
    # a program that never asked for them would be a regression.
    page, base = phone
    lines = "".join("line %03d\r\n" % i for i in range(200))
    sent = _open_tile(page, base, lines)
    sent.clear()

    before = page.evaluate("() => document.querySelector('.xterm-viewport').scrollTop")
    page.click('#term-mobile-toolbar button[data-scroll="-1"]')
    page.wait_for_timeout(300)
    after = page.evaluate("() => document.querySelector('.xterm-viewport').scrollTop")

    assert after < before
    assert sent == []
