"""The terminal page is exactly the viewport, not a nav-height taller.

The page sets `height: 100dvh` on the body, but the body also carries
`padding-top: var(--nav-h)` for the fixed nav and the default content-box
sizing, so the document came out 100dvh *plus* one nav height. Desktop
never showed it: `overflow: hidden` propagates to the viewport and there is
nothing there to move the page. A phone moves it anyway -- the URL bar
retracting re-resolves dvh, and the overscroll that comes with it lands the
document 56px up and leaves it there. From then on the fixed nav sits over
the top 56px of the shell, which is the entire session-list header (back
arrow, Active/Suspended tabs, + new, refresh), and the 56px it vacated at
the bottom shows the site's hex-pattern body background under the terminal.

So this measures the document against the viewport at a phone size rather
than reading the CSS: the defect is a box-model sum, and only layout adds
it up.
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

MEASURE = """
() => {
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return {top: b.top, bottom: b.bottom, height: b.height};
  };
  return {
    innerHeight: window.innerHeight,
    scrollHeight: document.documentElement.scrollHeight,
    scrollY: window.scrollY,
    nav: box('nav'),
    app: box('.term-app'),
    listHead: box('.term-list-head'),
  };
}
"""


@pytest.fixture(scope="module")
def live_app():
    """The real app on a real port -- the page has to load its own assets."""
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
def phone(live_app):
    """The session list as a phone sees it, with the API stubbed."""
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


def test_the_page_is_no_taller_than_the_screen(phone):
    """A document taller than the viewport is a page a phone can shove."""
    m = phone.evaluate(MEASURE)
    # The bug measured 836 against a 780 screen: one nav height of slack.
    assert m["scrollHeight"] <= m["innerHeight"]


def test_the_shell_fills_the_screen_under_the_nav(phone):
    m = phone.evaluate(MEASURE)
    assert m["app"]["top"] == pytest.approx(m["nav"]["bottom"], abs=1)
    assert m["app"]["bottom"] == pytest.approx(m["innerHeight"], abs=1)


def test_the_session_controls_survive_a_shove(phone):
    """Scrolling must not slide the list header up behind the fixed nav.

    This is the visible symptom: no back arrow, no Active/Suspended tabs,
    no + new -- so a phone could open a session but never leave it.
    """
    phone.evaluate("() => window.scrollTo(0, 500)")
    phone.wait_for_timeout(100)
    m = phone.evaluate(MEASURE)
    assert m["scrollY"] == 0
    assert m["listHead"]["top"] >= m["nav"]["bottom"] - 1
    assert m["listHead"]["bottom"] <= m["innerHeight"]
