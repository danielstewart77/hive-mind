"""The pty is spawned at the tile's real geometry, not xterm's default.

The attach URL's cols/rows become the pty's spawn size, so the tile has to
be measured before the socket opens. Mounting into a stage that hasn't
been laid out yet — on mobile that's every tile, since the rail is what's
on screen when you tap a card — leaves the box at zero, FitAddon declines
to guess, and xterm keeps 80x24. Every attach in the mind's log carried
cols=80&rows=24 while phone tiles render around 44 columns, and the TUI
painted its first screen almost twice as wide as the screen showing it.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import BASE_DIR


TERMINAL_HTML = (BASE_DIR / "templates" / "terminal.html").read_text(encoding="utf-8")


def _body(name: str) -> str:
    """Source of `function <name>(` up to the next top-level function."""
    start = TERMINAL_HTML.index("function " + name + "(")
    rest = TERMINAL_HTML[start + 1:]
    end = rest.find("\n        function ")
    return rest if end == -1 else rest[:end]


def test_mount_measures_before_attaching():
    block = TERMINAL_HTML[TERMINAL_HTML.index("mount: function ()"):]
    block = block[: block.index("},")]
    assert "fitWhenSized(attach)" in block
    # A bare fit() here is the bug: it measures a box that has no size yet.
    assert not re.search(r"\bfitAddon\.fit\(\)", block)
    assert not re.search(r"^\s*attach\(\);", block, re.MULTILINE)


def test_fit_when_sized_waits_for_a_real_box():
    body = _body("fitWhenSized")
    assert "clientWidth > 0" in body and "clientHeight > 0" in body
    assert "requestAnimationFrame" in body
    # It must give up eventually rather than leaving a tile unattached
    # forever if the box never gains a size.
    assert re.search(r"n\s*>=\s*\d+", body)


def test_attach_url_carries_the_measured_geometry():
    block = TERMINAL_HTML[TERMINAL_HTML.index("function attach()"):]
    block = block[: block.index("socket.onmessage")]
    assert "wsUrl(sessionId, term.cols, term.rows)" in block
    # and re-asserts it once the socket is live, in case the box changed
    # shape between the measurement and the connection completing
    assert "sendResize()" in block
