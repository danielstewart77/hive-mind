"""Dictating into a tile must not walk the terminal sideways.

While an IME composition is open, xterm parks its hidden textarea just past
the preview bubble and gives it the bubble's width. Once that bubble was
made to wrap at the tile, the bubble became as wide as the tile, so the
focused textarea landed a whole tile-width off the right edge. Nothing
clips it -- the tile's only containment is overflow: hidden, which is still
scrollable -- so the browser scrolled the terminal sideways to keep the
element it thought was being edited in view, and the content marched left
with every dictated word.

This is a real browser at a phone's viewport because the bug lives in the
interaction between xterm's inline styles, this repo's CSS and the
browser's scroll-into-view: nothing short of layout reproduces it.
"""

import json
import asyncio
import os
import socket
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

import auth  # noqa: E402
from hive_site import testing as accounts  # noqa: E402
import main as main_mod
import store  # noqa: E402

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
# Long enough that the bubble wraps and takes the full width of the tile,
# which is what inflated the textarea.
DICTATED = "this is what a whole dictated sentence looks like when it keeps going"


@pytest.fixture(scope="module")
def live_app():
    """The real app on a real port — the page has to load its own assets."""
    tmp = tempfile.mkdtemp()
    os.environ["HIVE_DB_PATH"] = os.path.join(tmp, "stb.db")
    os.environ["HIVE_DATABASE_URL"] = ""
    os.environ["HIVE_SECRET_KEY"] = "browser-test-secret"
    # The module's own server, so the module's own open: the store is not
    # opened on demand, and this fixture does not run the app's lifespan.
    asyncio.run(store.open())

    import uvicorn

    user = accounts.make_user("browser-test", "browser-pass", is_admin=True, replace=True)
    token = accounts.session_token(user)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(
        uvicorn.Config(main_mod.app, host="127.0.0.1", port=port, log_level="error")
    )
    serving = threading.Thread(target=server.run, daemon=True)
    serving.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.skip("app did not come up")

    yield f"http://127.0.0.1:{port}", token
    server.should_exit = True
    # Waited for, not fired and forgotten. The app's own lifespan owns the
    # store now, and its shutdown closes it — on the server's thread. Left
    # unjoined, that close lands after the next module has opened its own
    # store and every test in it then fails on a closed one.
    serving.join(timeout=10)
    if store.is_open():
        asyncio.run(store.close())


@pytest.fixture()
def tile(live_app):
    """One mounted terminal tile on a phone-sized screen, socket stubbed."""
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
        page = ctx.new_page()
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
        page.route_web_socket(
            "**/api/terminal/attach/**",
            lambda ws: ws.send("skippy@box:~$ tell me about the thing"),
        )
        page.goto(base + "/terminal")
        page.wait_for_selector(".term-card", timeout=15000)
        page.click(".term-card")
        page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
        page.wait_for_timeout(600)
        yield page
        browser.close()


def _compose(page, text):
    """Open an IME composition carrying `text`, as a soft keyboard does."""
    return page.evaluate(
        """(text) => {
            const host = document.querySelector('.term-xterm');
            const ta = host.querySelector('.xterm-helper-textarea');
            ta.focus();
            ta.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
            ta.value = text;
            ta.dispatchEvent(new CompositionEvent('compositionupdate', {data: text, bubbles: true}));
            const panel = document.querySelector('.term-panel');
            const bubble = host.querySelector('.composition-view');
            return {
                panelScrollWidth: panel.scrollWidth,
                panelClientWidth: panel.clientWidth,
                textareaRight: ta.getBoundingClientRect().right,
                bubbleWidth: bubble ? bubble.getBoundingClientRect().width : 0,
                screenRight: host.querySelector('.xterm-screen').getBoundingClientRect().right,
            };
        }""",
        text,
    )


def test_dictating_adds_no_sideways_overflow_to_the_tile(tile):
    state = _compose(tile, DICTATED)
    # The bug measured 682 against a 412 tile: a tile-width of scrollable
    # overflow, which is what the browser then panned into view.
    assert state["panelScrollWidth"] == state["panelClientWidth"]
    assert state["textareaRight"] <= state["screenRight"] + 1


def test_the_preview_bubble_still_gets_the_whole_tile_to_wrap_in(tile):
    # The pin above must not be paid for by un-wrapping the bubble: it is
    # the visible half of dictation and the reason the textarea inflates.
    state = _compose(tile, DICTATED)
    assert state["bubbleWidth"] > PHONE["width"] / 2


def test_the_terminal_is_never_wider_than_the_tile_that_holds_it(tile):
    # The measured fit is what the pty is spawned at, so a screen wider
    # than its box means the TUI is drawing where nobody can see it.
    fits = tile.evaluate(
        """() => {
            const host = document.querySelector('.term-xterm');
            const screen = host.querySelector('.xterm-screen').getBoundingClientRect();
            const viewport = host.querySelector('.xterm-viewport');
            return {screenWidth: screen.width, viewportWidth: viewport.clientWidth};
        }"""
    )
    assert fits["screenWidth"] <= fits["viewportWidth"]
