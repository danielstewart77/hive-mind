"""Text that scrolls up has to leave the screen it scrolled off correct.

The artifacts on this terminal are invalidations the browser drops: the
buffer behind the garbage is already right, no later write touches those
rows, and the mess survives until something forces a re-render — which is
why dragging a selection across it brings the text back.

Two things stop that. The grid is painted by WebGL, so it is one canvas on
one composited layer rather than a few thousand row divs and cell spans
for an invalidation to go missing across, and there is no sub-cell slack
outside a painted surface for the stale strips either side of the grid to
live in. And every scroll ends in a repaint, coalesced onto a frame so
that a screen's worth of scrolling costs one repaint, not one per line.

WebGL can still be unavailable or lose its context, and xterm then falls
back to its DOM renderer — the artifact-prone one. Everything that guards
that renderer is still here and still tested, on a tile driven into the
fallback deliberately.

These drive a real browser because the bug is in what the page paints.
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


class Tile:
    """A mounted tile, plus the pty end of its socket to write more from."""

    def __init__(self, page):
        self.page = page
        self.ws = None

    def pty_writes(self, text):
        """Send bytes as the pty would — a binary frame on the attach socket."""
        assert self.ws is not None, "the tile never attached"
        self.ws.send(text.encode())

    # the tests drive the page directly for everything else
    def __getattr__(self, name):
        return getattr(self.page, name)


def _open_tile(live_app):
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
        tile = Tile(page)
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
        def _attach(ws):
            tile.ws = ws
            ws.send(PTY_TEXT.encode())

        page.route_web_socket("**/api/terminal/attach/**", _attach)
        page.goto(base + "/terminal")
        page.wait_for_selector(".term-card", timeout=15000)
        page.click(".term-card")
        page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
        page.wait_for_timeout(800)
        yield tile
        browser.close()


@pytest.fixture()
def tile(live_app):
    yield from _open_tile(live_app)


@pytest.fixture()
def dom_tile(live_app):
    """A tile driven off WebGL and onto the fallback DOM renderer.

    Losing the context is a real event — a GPU reset, a driver update, a
    browser reclaiming contexts from a backgrounded tab — and the tile has
    to stay readable through it.
    """
    for one in _open_tile(live_app):
        _lose_webgl(one)
        one.page.wait_for_selector(".term-panel .xterm-rows", timeout=5000)
        one.page.wait_for_timeout(200)
        yield one


def _lose_webgl(page):
    """Take the GL context away the way a GPU reset would.

    Headless Chromium runs on SwiftShader, which offers no
    WEBGL_lose_context extension, so where the real thing isn't available
    fall back to dispatching the event a real loss delivers — that event
    is what the addon actually listens for.
    """
    lost = page.evaluate(
        """() => {
            let hit = 0;
            for (const c of document.querySelectorAll('.xterm-screen canvas')) {
                const gl = c.getContext('webgl2') || c.getContext('webgl');
                if (!gl) continue;
                const ext = gl.getExtension('WEBGL_lose_context');
                if (ext) ext.loseContext();
                else c.dispatchEvent(new Event('webglcontextlost', {cancelable: true}));
                hit++;
            }
            return hit;
        }"""
    )
    if not lost:
        pytest.skip("no WebGL context to lose in this browser")


def _renderer(page):
    (only,) = page.evaluate("() => window.rendererInfo()").values()
    return only


def _on_screen(page):
    """What the terminal holds for the visible rows, whatever paints them."""
    (only,) = page.evaluate("() => window.screenText()").values()
    return only


def _painted(page):
    return page.evaluate("() => document.querySelector('.xterm-rows').textContent")


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


# ── the grid is one canvas ────────────────────────────────────────────


def test_the_grid_is_painted_by_webgl(tile):
    # One canvas on one composited layer is the fix: no per-row elements
    # for an invalidation to go missing across.
    info = _renderer(tile)
    if not info["webgl"]:
        pytest.skip("browser has no WebGL")
    assert info["canvases"] >= 1


def test_the_pty_line_reaches_the_screen(tile):
    assert PTY_TEXT in _on_screen(tile)


def test_scrolling_repaints_once_a_frame_not_once_a_line(tile):
    # A build log scrolls hundreds of lines a second. Repainting each one
    # would stall the tab, and a frame can only show one screen anyway.
    lines = 400
    before = _renderer(tile)["repaints"]
    tile.pty_writes("".join("scrolling line %d\r\n" % i for i in range(lines)))
    tile.page.wait_for_timeout(1000)
    after = _renderer(tile)["repaints"]
    assert "scrolling line %d" % (lines - 1) in _on_screen(tile)
    assert after > before, "scrolled text never triggered a repaint"
    assert after - before < lines / 4, "repaint fired per line instead of per frame"


def test_the_tile_clips_its_own_paint(tile):
    # The grid is narrower than the tile by whatever a whole number of
    # cells leaves over, and the strips of stale text either side of the
    # terminal sit in that slack. Containment keeps paint inside.
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


# ── and when WebGL goes away ──────────────────────────────────────────


def test_a_lost_context_leaves_a_readable_tile(dom_tile):
    # The point of the fallback: the conversation stays legible even though
    # the renderer under it changed mid-session.
    assert _renderer(dom_tile)["webgl"] is False
    assert PTY_TEXT in _painted(dom_tile)


def test_clicking_the_tile_repaints_it_from_the_buffer(dom_tile):
    _go_stale(dom_tile)
    assert GARBAGE in _painted(dom_tile)
    dom_tile.dispatch_event(".term-panel", "mousedown")
    dom_tile.page.wait_for_timeout(200)
    painted = _painted(dom_tile)
    assert PTY_TEXT in painted and GARBAGE not in painted


def test_returning_to_the_tab_repaints_every_tile(dom_tile):
    # A backgrounded tab has rAF throttled, and xterm's render debouncer
    # rides on rAF: rows dirtied while away can go unpainted forever.
    _go_stale(dom_tile)
    dom_tile.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    dom_tile.page.wait_for_timeout(200)
    painted = _painted(dom_tile)
    assert PTY_TEXT in painted and GARBAGE not in painted


def test_a_resize_that_changes_the_grid_repaints(dom_tile):
    _go_stale(dom_tile)
    dom_tile.set_viewport_size({"width": 760, "height": DESKTOP["height"]})
    dom_tile.page.wait_for_timeout(2600)  # the refit sweep runs every 2s
    painted = _painted(dom_tile)
    assert PTY_TEXT in painted and GARBAGE not in painted


def test_paint_check_reports_a_clean_tile_as_clean(dom_tile):
    (only,) = dom_tile.evaluate("() => window.paintCheck()").values()
    assert only["drift"] == 0
    assert only["rows"] > 0


def test_paint_check_catches_a_row_the_dom_has_lost(dom_tile):
    # Tell "xterm painted the wrong thing" apart from "the DOM is right and
    # the pixels are stale", without selecting the text — selecting forces a
    # repaint and destroys the evidence.
    _go_stale(dom_tile)
    (only,) = dom_tile.evaluate("() => window.paintCheck()").values()
    assert only["drift"] >= 1
    sample = only["samples"][0]
    assert GARBAGE in sample["dom"]
    assert GARBAGE not in sample["buffer"]


def test_the_sweep_runs_without_the_debug_flag(dom_tile):
    # The sweep is what keeps a fallen-back tile readable while text
    # scrolls, so it has to run on a plain page load — not only when
    # someone thinks to ask for it.
    _go_stale(dom_tile)
    assert GARBAGE in _painted(dom_tile)
    dom_tile.page.wait_for_timeout(2600)
    painted = _painted(dom_tile)
    assert PTY_TEXT in painted and GARBAGE not in painted


def test_the_paint_sweep_reports_nothing_for_a_webgl_tile(tile):
    # A canvas has no rows to read back, so the sweep has nothing to say —
    # and must not mistake that for a tile it should be repainting.
    if not _renderer(tile)["webgl"]:
        pytest.skip("browser has no WebGL")
    (only,) = tile.evaluate("() => window.paintCheck()").values()
    assert only is None


def test_paintcheck_mode_puts_the_count_in_the_tile_header(dom_tile):
    # Reading a number off the header is the whole interface: no devtools,
    # no typing into a terminal that cannot be copied out of.
    dom_tile.page.goto(dom_tile.page.url.split("#")[0] + "?paintcheck=1")
    dom_tile.page.wait_for_selector(".term-card", timeout=15000)
    dom_tile.page.click(".term-card")
    dom_tile.page.wait_for_selector(".term-panel .xterm-viewport", timeout=15000)
    # Let the socket finish attaching first — the header carries the attach
    # state too, and a late "attached" would overwrite the readout.
    dom_tile.page.wait_for_timeout(800)
    _lose_webgl(dom_tile.page)
    dom_tile.page.wait_for_selector(".term-panel .xterm-rows", timeout=5000)
    dom_tile.page.wait_for_timeout(2600)  # the sweep runs every 2s
    assert "dom-drift" in dom_tile.page.eval_on_selector(
        ".term-panel-info", "e => e.innerText"
    )
