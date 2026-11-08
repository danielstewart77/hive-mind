"""A refresh must land back on the open session, not the agent list.

The terminal records what a tile is showing in the URL fragment
(`#s=<id>`), updated with replaceState as tiles open and close. On a phone
that is the difference between a refresh dropping the user back on the
agent list and a refresh reopening the session they were reading. A real
browser because the whole point is that the fragment survives a full page
navigation and drives the boot path.
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
        "id": "sess-alpha",
        "mind_id": "mind-uuid",
        "mind_name": "skippy",
        "status": "running",
        "short_id": "alpha",
    },
    {
        "id": "sess-beta",
        "mind_id": "mind-uuid",
        "mind_name": "skippy",
        "status": "running",
        "short_id": "beta",
    },
]


@pytest.fixture(scope="module")
def live_app():
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


def _wire(page):
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
        lambda ws: ws.send("skippy@box:~$ ready"),
    )


def _mobile_ctx(browser, token):
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
    return ctx


def test_opening_a_session_writes_it_to_the_fragment(live_app):
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = _mobile_ctx(browser, token).new_page()
        _wire(page)
        page.goto(base + "/terminal")
        page.wait_for_selector(".term-card", timeout=15000)
        # No tile open yet: clean URL, no fragment.
        assert "#s=" not in page.url

        page.click(".term-card >> nth=0")
        page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
        assert page.evaluate("() => location.hash") == "#s=sess-alpha"

        # Backing out clears it again rather than leaving a stale #s=.
        page.click(".term-panel-back")
        page.wait_for_timeout(200)
        assert page.evaluate("() => location.hash") == ""
        browser.close()


def test_a_refresh_reopens_the_fragment_session_on_mobile(live_app):
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = _mobile_ctx(browser, token).new_page()
        _wire(page)
        # Land straight on a deep link, as a refresh with a set fragment does.
        page.goto(base + "/terminal#s=sess-beta")
        page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
        info = page.text_content(".term-panel-info")
        assert "beta" in info or "skippy" in info
        # The stage is up, not the list.
        assert page.evaluate("() => document.getElementById('term-app').dataset.view") == "stage"
        browser.close()


def test_a_fragment_naming_a_dead_session_falls_back_to_the_list(live_app):
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = _mobile_ctx(browser, token).new_page()
        _wire(page)
        page.goto(base + "/terminal#s=sess-ghost")
        page.wait_for_selector(".term-card", timeout=15000)
        page.wait_for_timeout(400)
        # Nothing to reopen: the list shows rather than a blank stage.
        assert page.evaluate("() => document.getElementById('term-app').dataset.view") == "list"
        assert page.query_selector(".term-panel") is None
        browser.close()
