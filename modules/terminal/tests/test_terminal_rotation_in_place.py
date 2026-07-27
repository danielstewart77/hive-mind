"""A rotation that keeps the pane.

When a session's context fills, the gateway retires it and starts a
successor. For a browser tile that is a swap under the user's hands: the
terminal, the pty and the socket all survive, only the session id changes.
The gateway says so with a TEXT frame on the same socket — terminal output
is always BINARY, so the two can never be confused — and the tile has to
relabel itself in place. Tearing the socket down and reattaching is the
failure this exists to prevent: it reads to the user as being thrown out
of the conversation mid-sentence.

A real browser, because the thing under test is what the page does to a
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


def test_a_rotation_frame_moves_the_tile_to_the_successor(page):
    page, base = page
    sockets = _open_tile(page, base)

    sockets[0].send(json.dumps({"type": "session_rotated", "session_id": "sess-new"}))
    page.wait_for_timeout(400)

    assert page.evaluate(
        "() => document.querySelector('.term-panel').dataset.sessionId"
    ) == "sess-new"
    # One socket for the whole rotation: the tile must not have reattached.
    assert len(sockets) == 1


def test_the_same_socket_goes_on_carrying_the_conversation(page):
    page, base = page
    sockets = _open_tile(page, base)

    sockets[0].send(json.dumps({"type": "session_rotated", "session_id": "sess-new"}))
    page.wait_for_timeout(300)
    sockets[0].send(b"after the rotation\r\n")
    page.wait_for_timeout(400)

    assert "after the rotation" in _screen(page)


def test_a_text_frame_that_is_not_a_control_message_is_not_printed(page):
    """The control channel is JSON only. Anything else on it is a bug
    upstream, and echoing it into the pane corrupts the screen."""
    page, base = page
    sockets = _open_tile(page, base)

    sockets[0].send("not json at all")
    page.wait_for_timeout(400)

    assert "not json at all" not in _screen(page)
