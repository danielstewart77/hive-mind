"""The soft keyboard shortens the terminal instead of covering it.

A phone keyboard is painted over the page. The layout viewport does not
move, so every dvh-sized box -- the terminal shell among them -- keeps its
full height with the keyboard parked over its bottom third: over the
prompt, over the cursor, over the line being typed. The only way to read
what you were typing was to drag the page up by hand.

Two independent mechanisms, because no single one covers both engines.
`interactive-widget=resizes-content` makes the keyboard shorten the layout
viewport where it is honoured; iOS Safari ignores the key outright and
shrinks only the visual viewport, so the shell's height is driven from
`visualViewport.height`, which is correct on both.

The browser test simulates the shrink directly -- headless Chromium has no
soft keyboard, but the observable effect of one is a smaller visual
viewport, and what matters is that the shell follows it down.
"""

import json
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
import main as main_mod  # noqa: E402

BASE_DIR = main_mod.BASE_DIR
PHONE = {"width": 412, "height": 780}
KEYBOARD_PX = 320
SESSIONS = [
    {
        "id": "sess-1",
        "mind_id": "mind-uuid",
        "mind_name": "skippy",
        "status": "running",
        "short_id": "sess-1",
    }
]


def test_the_viewport_meta_lets_the_keyboard_resize_the_page():
    layout = (BASE_DIR / "templates" / "layout.html").read_text(encoding="utf-8")
    assert "interactive-widget=resizes-content" in layout


def test_the_shell_height_follows_the_visual_viewport():
    """CSS reads --app-h; the page script is what keeps it current."""
    css = (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")
    terminal = (BASE_DIR / "templates" / "terminal.html").read_text(encoding="utf-8")
    # A bare 100dvh here is the bug: it does not move for a keyboard.
    assert "height: var(--app-h, 100dvh)" in css
    assert "calc(var(--app-h, 100dvh) - var(--nav-h))" in css
    assert 'setProperty(\n            "--app-h"' in terminal
    assert 'visualViewport.addEventListener("resize", syncAppHeight)' in terminal
    # iOS scrolls the visual viewport rather than resizing the layout one,
    # which slides the fixed nav off the top until the page is pinned back.
    assert 'visualViewport.addEventListener("scroll", syncAppHeight)' in terminal


@pytest.fixture(scope="module")
def live_app():
    tmp = tempfile.mkdtemp()
    os.environ["HIVE_DB_PATH"] = os.path.join(tmp, "stb.db")
    os.environ["HIVE_SECRET_KEY"] = "browser-test-secret"

    import uvicorn

    user = auth.create_user("browser-test", "browser-pass", is_admin=True, replace=True)
    token = auth.create_session_token(user)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(
        uvicorn.Config(main_mod.app, host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
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
        page.goto(base + "/terminal")
        page.wait_for_selector(".term-card", timeout=15000)
        page.wait_for_timeout(300)
        yield page
        browser.close()


def _open_keyboard(page, pixels):
    """Shrink the visual viewport the way a soft keyboard does."""
    page.evaluate(
        """(px) => {
            const vv = window.visualViewport;
            Object.defineProperty(vv, 'height', {
                configurable: true, get: () => window.innerHeight - px,
            });
            vv.dispatchEvent(new Event('resize'));
        }""",
        pixels,
    )
    page.wait_for_timeout(200)


def _shell(page):
    return page.evaluate(
        """() => {
            const b = document.querySelector('.term-app').getBoundingClientRect();
            return {top: b.top, bottom: b.bottom, height: b.height,
                    innerHeight: window.innerHeight};
        }"""
    )


def test_the_shell_clears_the_keyboard(phone):
    before = _shell(phone)
    _open_keyboard(phone, KEYBOARD_PX)
    after = _shell(phone)
    # It used to keep every pixel and let the keyboard sit on top of the
    # bottom 320 of them.
    assert after["bottom"] <= before["innerHeight"] - KEYBOARD_PX + 1
    assert after["height"] == pytest.approx(before["height"] - KEYBOARD_PX, abs=2)


def test_the_shell_comes_back_when_the_keyboard_closes(phone):
    before = _shell(phone)
    _open_keyboard(phone, KEYBOARD_PX)
    _open_keyboard(phone, 0)
    after = _shell(phone)
    assert after["height"] == pytest.approx(before["height"], abs=1)
    assert after["top"] == pytest.approx(before["top"], abs=1)
