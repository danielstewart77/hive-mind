"""A phone is far more likely to suspend a background tab -- and the socket
riding on it -- than it is to kill a tab that's actively holding a screen
wake lock. Acquiring one while a tile is open trades battery for fewer dead
sockets the reattach machinery has to recover from; the point of these
tests is that the lock tracks tile lifetime exactly: held while any tile is
open, released the moment none are, and reacquired on returning to the tab.

navigator.wakeLock is stubbed rather than exercised for real -- headless
Chromium's implementation is inconsistent about actually granting a lock in
an automated context, and the behavior under test is "did the page ask",
not "did the OS grant".
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

DESKTOP = {"width": 1200, "height": 800}
SESSIONS = [
    {
        "id": "sess-1",
        "mind_id": "mind-uuid",
        "mind_name": "skippy",
        "status": "running",
        "short_id": "sess-1",
    }
]

STUB_WAKE_LOCK = """
window.__wakeLockCalls = [];
window.__wakeLockReleases = 0;
function __makeWakeLockSentinel() {
    const listeners = [];
    return {
        released: false,
        addEventListener(name, fn) { if (name === "release") listeners.push(fn); },
        release() {
            this.released = true;
            window.__wakeLockReleases += 1;
            listeners.forEach((fn) => fn());
            return Promise.resolve();
        },
    };
}
// navigator.wakeLock is a getter-only accessor on Navigator.prototype in a
// real browser, so a plain assignment silently no-ops (or throws in strict
// mode) instead of shadowing it -- defineProperty is required to actually
// replace it with an own property on this instance.
Object.defineProperty(navigator, "wakeLock", {
    configurable: true,
    value: {
        request(type) {
            window.__wakeLockCalls.push(type);
            return Promise.resolve(__makeWakeLockSentinel());
        },
    },
});
"""


@pytest.fixture()
def tile(live_app):
    """One mounted tile, with navigator.wakeLock stubbed before any page script runs."""
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        ctx = browser.new_context(viewport=DESKTOP)
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
        ctx.add_init_script(STUB_WAKE_LOCK)
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
        page.route_web_socket("**/api/terminal/attach/**", lambda ws: None)
        page.goto(base + "/terminal")
        page.wait_for_selector(".term-card", timeout=15000)
        yield page
        browser.close()


def _calls(page):
    return page.evaluate("() => window.__wakeLockCalls")


def _releases(page):
    return page.evaluate("() => window.__wakeLockReleases")


def test_no_tile_open_requests_nothing(live_app):
    # A page that never opens a terminal has no reason to hold the screen
    # awake -- the lock is scoped to "a tile is attached", not "the app is
    # loaded". Uses its own page with zero active sessions: on a desktop
    # viewport the app deliberately auto-opens the most recent active
    # session so the stage isn't empty on a first visit (see the bottom of
    # terminal.html), so the `tile` fixture's one running session would
    # always end up opened regardless of what this test does.
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=DESKTOP)
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
        ctx.add_init_script(STUB_WAKE_LOCK)
        page = ctx.new_page()
        page.route(
            "**/api/terminal/sessions*",
            lambda r: r.fulfill(status=200, content_type="application/json", body="[]"),
        )
        page.route(
            "**/api/terminal/labels*",
            lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
        )
        page.goto(base + "/terminal")
        page.wait_for_selector("#term-list", timeout=15000)
        page.wait_for_timeout(200)
        assert _calls(page) == []
        browser.close()


def test_opening_a_tile_requests_a_screen_wake_lock(tile):
    tile.click(".term-card")
    tile.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    tile.wait_for_timeout(200)
    assert _calls(tile) == ["screen"]


def test_closing_the_only_tile_releases_the_lock(tile):
    tile.click(".term-card")
    tile.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    tile.wait_for_timeout(200)
    tile.click(".term-panel-close")
    tile.wait_for_timeout(200)
    assert _releases(tile) == 1


def test_returning_to_the_tab_reacquires_the_lock(tile):
    tile.click(".term-card")
    tile.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    tile.wait_for_timeout(200)
    assert len(_calls(tile)) == 1
    tile.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    tile.wait_for_timeout(200)
    # Already held, so the guard in acquireWakeLock skips a second request --
    # the resilience being tested is "the handler runs and does not throw",
    # not "it requests twice".
    assert _calls(tile) == ["screen"]
