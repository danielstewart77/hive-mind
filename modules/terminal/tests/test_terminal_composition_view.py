"""The IME preview bubble wraps at the tile, not at the cursor's leftovers.

xterm paints in-progress soft-keyboard and voice-typing text into a
.composition-view div that lives inside .xterm-helpers. Every child of that
helper layer is absolutely positioned, so the layer itself computes to zero
width, and the bubble is absolutely positioned at the cursor's x offset
inside it. Allowing the bubble to wrap without fixing either of those means
a percentage cap resolves against zero and shrink-to-fit is handed only the
sliver of room to the right of the cursor: Android voice typing came out as
one character per line running straight down the tile.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from main import BASE_DIR


STYLE_CSS = (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """Declarations of the last rule with this exact selector."""
    matches = re.findall(
        re.escape(selector) + r"\s*\{([^}]*)\}", STYLE_CSS
    )
    assert matches, f"no rule for {selector}"
    return matches[-1]


def test_helper_layer_is_stretched_so_percentages_resolve():
    # Without this the bubble's max-width is a percentage of zero.
    helpers = _rule(".term-panel .xterm .xterm-helpers")
    assert re.search(r"\bleft:\s*0\b", helpers)
    assert re.search(r"\bright:\s*0\b", helpers)


def test_bubble_is_pinned_left_over_xterms_inline_offset():
    # xterm rewrites `left` inline on every composition update, so only an
    # !important declaration keeps the bubble off the cursor's x offset.
    view = _rule(".term-panel .xterm .composition-view")
    left = re.search(r"\bleft:\s*([^;]+);", view)
    assert left, "composition-view must pin its left edge"
    assert "!important" in left.group(1)


def test_bubble_wraps_within_the_tile():
    view = _rule(".term-panel .xterm .composition-view")
    assert re.search(r"white-space:\s*pre-wrap", view)
    assert re.search(r"word-break:\s*break-word", view)
    assert re.search(r"max-width:\s*calc\(100% - 1rem\)", view)


def test_the_bubble_rule_still_outranks_xterms_own():
    # xterm.css ships `.xterm .composition-view { white-space: nowrap }`;
    # the override wins on specificity rather than on load order.
    assert ".term-panel .xterm .composition-view" in STYLE_CSS
