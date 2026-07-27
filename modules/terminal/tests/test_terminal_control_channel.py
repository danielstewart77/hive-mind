"""The attach socket's two channels.

Terminal output is always BINARY. TEXT frames are the gateway's control
channel — never output, so the tile must not print them. A rotation is not
on this channel: it replaces the conversation behind a session, not the
session, so the tile keeps its id and its socket and is told nothing.

A real browser, because the thing under test is what the page does with a
live WebSocket it is already holding.
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

SESSIONS = [
    {
        "id": "sess-old",
        "mind_id": "mind-uuid",
        "mind_name": "skippy",
        "status": "running",
        "short_id": "sess-old",
    }
]


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
def page(live_app):
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        ctx = browser.new_context(viewport={"width": 1100, "height": 800})
        ctx.add_cookies([{
            "name": auth.SESSION_COOKIE_NAME, "value": token,
            "domain": "127.0.0.1", "path": "/",
        }])
        yield ctx.new_page(), base
        browser.close()


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
    return page.evaluate(
        "() => document.querySelector('.term-panel .xterm-rows').innerText"
    )


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
