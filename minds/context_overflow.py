"""Whether a harness's failure is the model's context window overflowing.

Every harness words the same failure its own way — Claude says the prompt is
too long, codex that the input exceeds the context window, the proxy passes
the upstream's ``context_length_exceeded`` through — and the gateway acts on
one code for all of them: an overflow rotates the conversation at once and
re-sends the message, where any other failure is reported and left alone. So
each adapter maps its own wording onto ``CONTEXT_WINDOW_EXCEEDED`` and nothing
downstream reads the text.

The patterns are dsh's own classifier (``packages/llm/llm/src/error.ts``), so a
phrase dsh calls an overflow is one this side calls an overflow too, plus the
bare sentences the Claude and Codex CLIs report.
"""

from __future__ import annotations

import re

CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"

_PATTERNS = (
    re.compile(
        r"(?:^|[^a-z0-9])context[\s_-](?:length|window)[\s_-]"
        r"(?:exceed(?:ed|s)?|overflow(?:ed)?|limit[\s_-]exceeded)(?:$|[^a-z0-9])",
        re.I,
    ),
    re.compile(r"\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+(?:length|window)\b",
               re.I),
    re.compile(
        r"\b(?:request|prompt|input|messages?)\s+(?:is\s+|are\s+)?too\s+(?:large|long)\s+for\s+"
        r"(?:(?:this|the)\s+)?(?:model(?:'s)?\s+)?context(?:\s+window)?\b",
        re.I,
    ),
    re.compile(r"\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+"
               r"(?:this|the)\s+model\b", re.I),
    re.compile(
        r"\b(?:input|prompt|request|messages?)\b.{0,40}"
        r"\b(?:exceed(?:s|ed)?|overflows?|is\s+larger\s+than)\b.{0,40}"
        r"\b(?:the\s+)?(?:model(?:'s)?\s+)?context(?:\s+(?:length|window))?\b",
        re.I,
    ),
    # Codex's own sentence for a turn its window could not hold.
    re.compile(r"\bran out of room in the model'?s context window\b", re.I),
    # The Claude CLI's own sentence for a request the API refused as too big.
    re.compile(r"^\W*prompt is too long\b", re.I | re.M),
)


def is_overflow(*details: object) -> bool:
    """True when any of ``details`` names the context window being exceeded."""
    text = "\n".join(str(d) for d in details if d)
    if CONTEXT_WINDOW_EXCEEDED in text:
        return True
    return any(pattern.search(text) for pattern in _PATTERNS)
