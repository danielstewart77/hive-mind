"""The attach socket's two channels.

Terminal output is always BINARY. TEXT frames are the gateway's control
channel — never output, so the tile must not print them. A rotation speaks
on it: the session, the tile and the socket all survive one, but the pane is
respawned and its screen and scrollback go with it, so the gateway hands
over the exchange that was on that screen for the tile to draw beside the
terminal.

A real browser, because the thing under test is what the page does with a
live WebSocket it is already holding.
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
import main as main_mod  # noqa: E402

SESSIONS = [
    {
        "id": "sess-old",
        "mind_id": "mind-uuid",
        "mind_name": "skippy",
        "status": "running",
        "short_id": "sess-old",
    }
]


def _open_tile(page, base):
    """Mount one tile and hand back its live server-side socket."""
    sockets = []

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
        sockets.append(ws)
        ws.send(b"first paint\r\n")  # output is BINARY, always

    page.route_web_socket("**/api/terminal/attach/**", handle_ws)
    page.goto(base + "/terminal")
    page.wait_for_selector(".term-card", timeout=15000)
    page.click(".term-card")
    page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    page.wait_for_timeout(600)
    assert sockets, "the tile never opened a socket"
    return sockets


def _screen(page) -> str:
    """What the terminal holds for the visible rows.

    Read out of the buffer rather than off the DOM: the grid is painted by
    WebGL onto a canvas, and there are no row elements to inspect.
    """
    return "\n".join(page.evaluate("() => window.screenText()").values())


def test_a_text_frame_is_never_printed_into_the_pane(page):
    """Terminal output is BINARY; TEXT is the gateway's control channel.
    Anything arriving on it is a control message, and echoing one into the
    pane corrupts the screen."""
    page, base = page
    sockets = _open_tile(page, base)

    sockets[0].send("not terminal output")
    page.wait_for_timeout(400)

    assert "not terminal output" not in _screen(page)


def test_binary_output_still_reaches_the_screen(page):
    page, base = page
    sockets = _open_tile(page, base)

    sockets[0].send(b"live output\r\n")
    page.wait_for_timeout(400)

    assert "live output" in _screen(page)


def test_a_rotation_shows_the_previous_exchange_and_can_be_dismissed(page):
    """The respawn takes the pane's screen with it, so the tile draws what
    was on it beside the terminal — the harness owns the pane and repaints
    over anything written there. Dismissing it clears it."""
    page, base = page
    sockets = _open_tile(page, base)

    sockets[0].send(json.dumps({
        "type": "session_rotated",
        "session_id": "sess-1",
        "exchange": {
            "user": "why did it go blank?",
            "assistant": "the pane was respawned",
        },
    }))
    page.wait_for_selector(".term-rotated:not([hidden])", timeout=5000)

    banner = page.inner_text(".term-rotated-body")
    assert "why did it go blank?" in banner
    assert "the pane was respawned" in banner
    # The recap is conversation text, never markup, and never terminal output.
    assert "why did it go blank?" not in _screen(page)

    page.click(".term-rotated-close")
    page.wait_for_selector(".term-rotated", state="hidden", timeout=5000)
    assert page.inner_text(".term-rotated-body").strip() == ""
