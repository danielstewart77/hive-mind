"""Web-terminal reattach routing.

Two halves of one fix: the server has to hand the browser session lineage
(``rotated_from``), and the browser has to route on it. Without lineage a
reconnecting terminal tile guessed at "some live session on this mind" and
adopted whichever sibling it found — crossing two conversations and
migrating one tile's name and colour onto the other's session.

The browser half lives in ``src/static/terminal-routing.js`` and is
exercised by ``tests/js/terminal_routing_test.mjs``, run here so one
``pytest`` covers both sides.
"""

import os
import re
import shutil
import subprocess
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def _fake_gateway(monkeypatch, sessions):
    async def _gateway_json(path, *a, **kw):
        if path == "/broker/minds":
            return [{"id": "mind-uuid", "name": "skippy"}]
        if path == "/sessions":
            return sessions
        return []

    monkeypatch.setattr(main_mod, "_gateway_json", _gateway_json)


def test_session_list_carries_lineage(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    _fake_gateway(monkeypatch, [
        {"id": "old", "mind_id": "mind-uuid", "status": "closed", "last_active": 1},
        {"id": "new", "mind_id": "mind-uuid", "status": "running",
         "last_active": 2, "rotated_from": "old"},
    ])

    rows = client.get("/api/terminal/sessions").json()

    by_id = {r["id"]: r for r in rows}
    assert by_id["new"]["rotated_from"] == "old"
    assert by_id["old"]["rotated_from"] == ""


def test_lineage_absent_is_empty_not_missing(tmp_path, monkeypatch):
    """Older gateway rows have no such column; the key must still exist so
    the browser's comparison is against "" rather than undefined."""
    client = _authed_client(tmp_path, monkeypatch)
    _fake_gateway(monkeypatch, [
        {"id": "solo", "mind_id": "mind-uuid", "status": "running", "last_active": 1},
    ])

    rows = client.get("/api/terminal/sessions").json()

    assert rows[0]["rotated_from"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_reattach_routing_rules():
    script = os.path.join(os.path.dirname(__file__), "js", "terminal_routing_test.mjs")
    result = subprocess.run(
        [shutil.which("node"), script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_template_uses_the_routing_module():
    """Guard against the inline guess creeping back into the template."""
    template = os.path.join(
        os.path.dirname(__file__), "..", "src", "templates", "terminal.html"
    )
    with open(template, encoding="utf-8") as fh:
        body = fh.read()

    assert "TerminalRouting.pickReattachTarget" in body
    assert "terminal-routing.js" in body
    # The old heuristic: any live session belonging to the same mind.
    assert "r.mind_id === mindId" not in body


def test_template_stops_retrying_a_mind_without_a_terminal():
    """A mind whose image predates the pty route refuses the handshake, and
    the gateway reports that as 4415. Treating it like a dropped socket span
    the tile between connecting and reattaching several times a second."""
    template = os.path.join(
        os.path.dirname(__file__), "..", "src", "templates", "terminal.html"
    )
    with open(template, encoding="utf-8") as fh:
        body = fh.read()

    assert "4415" in body
    assert "no terminal on this mind" in body
    # Retries to the same session go through the backoff, never straight
    # back into attach().
    assert "TerminalRouting.retryDelayMs" in body
    assert "cancelReattachLoop(); attach(); return;" not in body


def test_template_forwards_mobile_ime_text_before_enter():
    """Mobile soft keyboards route typed words through an IME composition
    that isn't closed before Enter fires; xterm.js discards that buffered
    text instead of sending it, so the whole line silently vanishes. Guard
    against the capture-phase forwarder (verified against the real vendored
    xterm.js with a scripted CompositionEvent/InputEvent replay) regressing
    or being dropped."""
    template = os.path.join(
        os.path.dirname(__file__), "..", "src", "templates", "terminal.html"
    )
    with open(template, encoding="utf-8") as fh:
        body = fh.read()

    assert 'e.key !== "Enter" && e.keyCode !== 13' in body
    # Must be capture-phase (the trailing `true`) on an ancestor of the
    # textarea (xtermEl), not the textarea itself -- a same-element listener
    # still runs after xterm's own, which is registered first.
    idx = body.index('xtermEl.addEventListener("keydown"')
    listener_call = body[idx : idx + 500]
    assert "}, true);" in listener_call


def test_enter_forwards_only_the_open_composition_not_the_whole_box():
    """xterm never clears its helper textarea between words, so the box
    holds the whole line while each composition has already gone to the pty
    as it ended. The first cut of the Enter forwarder sent the box
    wholesale, and the pty echoed the typed paragraph concatenated with a
    copy of itself. Only the open composition is owed."""
    template = os.path.join(
        os.path.dirname(__file__), "..", "src", "templates", "terminal.html"
    )
    with open(template, encoding="utf-8") as fh:
        body = fh.read()

    assert "TerminalRouting.pendingImeText(" in body
    # The whole value must never be what goes on the wire.
    assert "encode(helperTa.value)" not in body
    # The toolbar's return key bypasses xterm's keydown path entirely, so
    # it has to settle the same debt.
    assert 'if (text === "\\r") flushPendingIme();' in body


def test_vendored_xterm_still_exposes_the_composition_state_we_read():
    """`pendingImeText` is fed from xterm's own composition state rather
    than a private guess at it. Those are internals: if xterm is
    re-vendored and renames them, the terminal would silently go back to
    swallowing every dictated line. Fail here instead, pointing at
    `pendingImeText` in terminal.html."""
    bundle = os.path.join(
        os.path.dirname(__file__), "..", "src", "static", "vendor", "xterm", "xterm.js"
    )
    with open(bundle, encoding="utf-8") as fh:
        body = fh.read()

    for symbol in (
        "_compositionHelper",
        "_compositionPosition",
        "_isComposing",
        "_isSendingComposition",
        "_dataAlreadySent",
    ):
        assert symbol in body, f"vendored xterm no longer has {symbol}"


def test_composition_view_wraps_on_mobile():
    """xterm's IME preview bubble ships with white-space: nowrap and no
    width cap; on a phone it runs off the screen edge instead of wrapping."""
    css_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "static", "style.css"
    )
    with open(css_path, encoding="utf-8") as fh:
        body = fh.read()

    assert ".composition-view" in body
    assert "pre-wrap" in body


def test_terminal_refits_on_container_resize_not_just_window_resize():
    """A window "resize" event alone misses layout changes that don't
    resize the window itself -- a foldable unfolding, an on-screen keyboard
    opening. Each panel must watch its own tile's box via ResizeObserver so
    xterm's cols/rows actually grow when the tile does, instead of staying
    pinned to whatever size it was at mount."""
    template = os.path.join(
        os.path.dirname(__file__), "..", "src", "templates", "terminal.html"
    )
    with open(template, encoding="utf-8") as fh:
        body = fh.read()

    assert "new ResizeObserver(scheduleFit)" in body
    assert "resizeObserver.observe(xtermEl)" in body
    assert "resizeObserver.disconnect()" in body
    # The old global-only trigger must be gone, not just supplemented --
    # leaving it would be redundant dead weight now that each panel fits
    # itself.
    assert 'window.addEventListener("resize"' not in body


def test_terminal_has_redundant_refit_signals_for_foldables():
    """ResizeObserver alone was verified (in a headless Chromium run
    against the real production page, both mount-then-widen and
    already-wide-then-mount) to refit correctly, but a foldable unfolding
    is reported to still leave the terminal at a stale narrower width on
    some real mobile browser that a Chromium run doesn't reproduce. Since
    the failing signal can't be pinned down without the device, refitting
    must not depend on any single browser event: visualViewport resize,
    orientationchange, and a periodic self-check are independent
    fallbacks alongside the per-panel ResizeObserver."""
    template = os.path.join(
        os.path.dirname(__file__), "..", "src", "templates", "terminal.html"
    )
    with open(template, encoding="utf-8") as fh:
        body = fh.read()

    assert "window.visualViewport.addEventListener(\"resize\"" in body
    assert 'window.addEventListener("orientationchange"' in body
    assert "setInterval(refitAllPanels" in body


def test_mobile_toolbar_can_reach_the_tiles_scrollback():
    """A phone has no visible scrollbar and no PageUp key. Without deliberate
    controls a reply taller than the screen is simply cut off, which is how
    a whole answer became unreadable on mobile. Where the scrolling has to
    happen depends on the buffer -- a TUI on the alternate screen holds its
    own history and only it can move it, so the panel routes through
    pageScrollAction rather than always moving the local viewport."""
    template = os.path.join(
        os.path.dirname(__file__), "..", "src", "templates", "terminal.html"
    )
    with open(template, encoding="utf-8") as fh:
        body = fh.read()

    assert 'data-scroll="-1"' in body and 'data-scroll="1"' in body
    assert "target.scrollPage(Number(btn.dataset.scroll));" in body
    # The scroll buttons route through scrollPage, not the byte-sending key path.
    idx = body.index('toolbar.addEventListener("click"')
    handler = body[idx : idx + 900]
    assert handler.index("dataset.scroll") < handler.index("sendBytes")
    assert "TerminalRouting.pageScrollAction(" in body

    css_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "static", "style.css"
    )
    with open(css_path, encoding="utf-8") as fh:
        css = fh.read()

    # Ten keys over five columns; four would strand two on a half row.
    assert re.search(
        r"\.term-mobile-toolbar\s*\{[^}]*grid-template-columns:\s*repeat\(5,", css
    )
    # A vertical drag scrolls the terminal instead of rubber-banding the page.
    viewport = re.search(
        r"\.term-panel \.xterm \.xterm-viewport\s*\{([^}]*)\}", css
    )
    assert viewport, "no viewport rule"
    assert "touch-action: pan-y" in viewport.group(1)
    assert "overscroll-behavior: contain" in viewport.group(1)
