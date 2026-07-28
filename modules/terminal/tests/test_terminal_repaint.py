"""Stale glyphs on screen have to be repaintable from the buffer.

xterm paints only the rows it has marked dirty, so an invalidation the
browser drops is never retried — the buffer behind the garbage is already
correct and no later write touches those rows again. That is why the
artifacts on this terminal survive indefinitely, and why dragging a
selection across them brings the text back correct: a selection forces
those rows to re-render. The cure is to force the same re-render at the
moments an invalidation is known to go missing, so reading the terminal
never depends on selecting it.

These drive a real browser because the bug is in what the page paints:
the check is that a row whose DOM has gone stale is restored from the
terminal's own buffer, which is precisely the observed failure.
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
# What the pty "sent". Reappearing after a repaint proves the text came
# back out of the buffer rather than from whatever was left on screen.
PTY_TEXT = "the-real-line-from-the-pty"
GARBAGE = "XXgarbageXX"


@pytest.fixture(scope="module")
def live_app():
    """The real app on a real port — the page has to load its own assets."""
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
def tile(live_app):
    """One mounted tile whose buffer holds a known line of pty output."""
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
        # Terminal output is always a BINARY frame; a TEXT frame is the
        # control channel and the tile deliberately never prints it.
        page.route_web_socket(
            "**/api/terminal/attach/**",
            lambda ws: ws.send(PTY_TEXT.encode()),
        )
        page.goto(base + "/terminal")
        page.wait_for_selector(".term-card", timeout=15000)
        page.click(".term-card")
        page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
        page.wait_for_timeout(800)
        yield page
        browser.close()


def _painted(page):
    return page.evaluate(
        "() => document.querySelector('.xterm-rows').textContent"
    )


def _go_stale(page):
    """Leave the DOM showing garbage while the buffer stays correct.

    This is the bug in one line: the rows on screen no longer match the
    rows in the buffer, and nothing in xterm will notice.
    """
    page.evaluate(
        """(garbage) => {
            const rows = document.querySelector('.xterm-rows');
            for (const row of rows.children) {
                if (row.textContent.trim()) { row.textContent = garbage; return; }
            }
        }""",
        GARBAGE,
    )


def test_the_pty_line_is_on_screen_to_begin_with(tile):
    assert PTY_TEXT in _painted(tile)


def test_clicking_the_tile_repaints_it_from_the_buffer(tile):
    _go_stale(tile)
    assert GARBAGE in _painted(tile)
    tile.dispatch_event(".term-panel", "mousedown")
    tile.wait_for_timeout(200)
    painted = _painted(tile)
    assert PTY_TEXT in painted and GARBAGE not in painted


def test_returning_to_the_tab_repaints_every_tile(tile):
    # A backgrounded tab has rAF throttled, and xterm's render debouncer
    # rides on rAF: rows dirtied while away can go unpainted forever.
    _go_stale(tile)
    tile.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    tile.wait_for_timeout(200)
    painted = _painted(tile)
    assert PTY_TEXT in painted and GARBAGE not in painted


def test_a_resize_that_changes_the_grid_repaints(tile):
    _go_stale(tile)
    tile.set_viewport_size({"width": 760, "height": DESKTOP["height"]})
    tile.wait_for_timeout(2600)  # the refit sweep runs every 2s
    painted = _painted(tile)
    assert PTY_TEXT in painted and GARBAGE not in painted


def test_the_tile_clips_its_own_paint(tile):
    # The grid is narrower than the tile by whatever a whole number of
    # cells leaves over, and the strips of stale text either side of the
    # terminal sit in that slack — no row covers them, so no repaint xterm
    # performs can reach them. Containment is what keeps paint inside.
    style = tile.evaluate(
        """() => {
            const s = getComputedStyle(document.querySelector('.term-xterm'));
            return {contain: s.contain, overflow: s.overflowX};
        }"""
    )
    assert "paint" in style["contain"]
    assert style["overflow"] == "hidden"


def test_the_terminal_still_fills_the_tile_it_is_clipped_to(tile):
    # Clipping must not be paid for by shrinking the terminal: a grid that
    # no longer fills the box is a pty spawned at the wrong geometry.
    box = tile.evaluate(
        """() => {
            const host = document.querySelector('.term-xterm');
            const style = getComputedStyle(host);
            const pad = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
            const screen = host.querySelector('.xterm-screen').getBoundingClientRect();
            return {screenWidth: screen.width, contentWidth: host.clientWidth - pad};
        }"""
    )
    # A whole-cell grid can never fill its box exactly, and FitAddon holds
    # back a scrollbar's width on top of that — together that is the couple
    # of characters of dead strip either side. It is slack, not a shortfall.
    assert box["screenWidth"] <= box["contentWidth"]
    assert box["screenWidth"] > box["contentWidth"] * 0.95


def test_paint_check_reports_a_clean_tile_as_clean(tile):
    report = tile.evaluate("() => window.paintCheck()")
    (only,) = report.values()
    assert only["drift"] == 0
    assert only["rows"] > 0


def test_paint_check_catches_a_row_the_dom_has_lost(tile):
    # The whole point: tell "xterm painted the wrong thing" apart from "the
    # DOM is right and the pixels are stale", without selecting the text —
    # selecting forces a repaint and destroys the evidence.
    _go_stale(tile)
    report = tile.evaluate("() => window.paintCheck()")
    (only,) = report.values()
    assert only["drift"] >= 1
    sample = only["samples"][0]
    assert GARBAGE in sample["dom"]
    assert GARBAGE not in sample["buffer"]


def test_paintcheck_mode_puts_the_count_in_the_tile_header(tile):
    # Reading a number off the header is the whole interface: no devtools,
    # no typing into a terminal that cannot be copied out of.
    tile.goto(tile.url.split("#")[0] + "?paintcheck=1")
    tile.wait_for_selector(".term-card", timeout=15000)
    tile.click(".term-card")
    tile.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    tile.wait_for_timeout(2600)
    assert "dom-drift" in tile.eval_on_selector(".term-panel-info", "e => e.innerText")


def test_the_sweep_runs_without_the_debug_flag(tile):
    # The sweep is what keeps the tile readable while text scrolls, so it
    # has to run on a plain page load — not only when someone thinks to
    # ask for it. A row that drifts heals on its own within a sweep.
    _go_stale(tile)
    assert GARBAGE in _painted(tile)
    tile.wait_for_timeout(2600)
    painted = _painted(tile)
    assert PTY_TEXT in painted and GARBAGE not in painted
