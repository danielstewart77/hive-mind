"""Rotation notices drawn over a terminal's harness, not into its pane.

A rotation respawns the pane's process, and `respawn-pane -k` takes the
pane's history with it — the tile goes blank and the conversation it was
holding is gone from the screen even though the session id, the tile and the
turn ledger all survived. What the user is left looking at is a fresh harness
with no indication that anything happened.

Text cannot simply be printed into the pane ahead of the harness. `claude`
runs on the alternate screen, so bytes written before its `exec` are wiped on
entry and never reach scrollback; `codex` leaves them in scrollback but paints
over them immediately. `status off` is set on the tmux server (the pane needs
that row, and the status line eats Escape), so a status-line message has
nowhere to render either.

`display-popup` is drawn by the tmux *client* on top of whatever the pane's
app is doing, which makes it the one surface that survives an alternate-screen
TUI in both harnesses. The client is the browser tile, so the popup is what
the tile shows.

This module is the rendering half — pure text and tmux argv, no I/O against a
live server — so both harness templates draw the same notices and a test can
assert on them without tmux.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

# A long reply would fill the popup with scrollback the user has to page
# through to reach the part they actually stopped at. The tail is what the
# conversation was doing when it rotated.
MAX_RECAP_LINES = 50

# A line cap alone does not bound a *prose* reply: one unbroken paragraph is
# a single line that wraps to a hundred rows. This is the second bound.
MAX_RECAP_CHARS = 4_000

# A popup holds the client's keyboard for as long as it is open, so it is
# never allowed to hold it forever — a tile whose user walked away comes back
# usable rather than wedged behind a modal nobody dismissed.
POPUP_TIMEOUT_SECONDS = 300

ROTATING_TEXT = "This session is rotating. A fresh conversation is starting."
ROTATED_TEXT = "This session has been rotated."

# Seconds a self-closing popup stays up. A popup holds the keyboard while it
# is open, so the "rotating" notice buys just enough time to be read.
NOTICE_SECONDS = 4

_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"


# Conversation text is replayed onto a terminal, so it is stripped of the
# escape sequences that would let it move the cursor, repaint the screen or
# retitle the window. `\n` and `\t` survive; everything else in C0, plus any
# ESC-introduced sequence, does not.
_CONTROL = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_][0-?]*[ -/]*[@-~]|\x1b.|[\x00-\x08\x0b-\x1f\x7f]")


def strip_control(text: str) -> str:
    """Render conversation text inert before it reaches a terminal."""
    return _CONTROL.sub("", text)


def tail_lines(
    text: str, limit: int = MAX_RECAP_LINES, chars: int = MAX_RECAP_CHARS
) -> str:
    """Keep the tail of ``text`` — the last ``limit`` lines, ``chars`` at most.

    Both bounds, because they catch different shapes: a transcript dump is
    long in lines, and a spoken-style reply is one very long line. The tail
    is what survives either way; it is where the conversation had got to.
    """
    lines = text.splitlines()
    dropped = 0
    if len(lines) > limit:
        dropped = len(lines) - limit
        text = "\n".join(lines[-limit:])
    if len(text) > chars:
        text = text[-chars:]
        dropped = max(dropped, 1)
    return f"[{dropped} earlier line(s) omitted]\n{text}" if dropped else text


def render_recap(
    exchange: dict | None,
    *,
    user_label: str = "user",
    assistant_label: str = "assistant",
) -> str:
    """The body of the post-rotation popup.

    ``exchange`` is ``{"user": ..., "assistant": ...}`` — the last thing said
    before the conversation turned over, replayed so the pane the user comes
    back to is not a blank one. Either side may be missing; with neither, the
    popup is the rotation line on its own, because a heading over nothing
    reads like something failed to load.
    """
    header = f"{_BOLD}── {ROTATED_TEXT} ──{_RESET}"
    exchange = exchange or {}
    user_text = strip_control(exchange.get("user") or "").strip()
    assistant_text = strip_control(exchange.get("assistant") or "").strip()
    if not user_text and not assistant_text:
        return header + "\n"

    parts = [header, ""]
    if user_text:
        parts += [f"{_DIM}{user_label}:{_RESET}", tail_lines(user_text), ""]
    if assistant_text:
        parts += [f"{_DIM}{assistant_label}:{_RESET}", tail_lines(assistant_text), ""]
    return "\n".join(parts)


def write_popup_body(directory: Path, key: str, text: str) -> Path:
    """Park the popup's text in a file so it never rides in the tmux command.

    Same reason the rotation seed does: tmux refuses a command past its own
    length limit, and a recap is as long as whatever was last said.
    """
    directory.mkdir(parents=True, exist_ok=True)
    body = directory / f"{key}.popup"
    body.write_text(text)
    return body


def popup_args(
    session_name: str,
    body: Path,
    *,
    hold: bool,
    width: str = "80%",
    height: str = "60%",
) -> list[str]:
    """tmux argv for a popup showing ``body``, read once and deleted.

    ``hold`` keeps the recap up until the user dismisses it — any single key,
    not Enter, so the first thing they type ends the popup instead of being
    swallowed by a line read. A passing notice instead closes itself after
    ``NOTICE_SECONDS``. Either way ``timeout`` bounds it: a popup holds the
    client's keyboard while it is open, and a tile whose user walked away
    must come back usable rather than wedged behind a modal.

    ``less`` when it exists, ``cat`` when it doesn't: a recap can be longer
    than the popup is tall, and with ``cat`` the heading and the user's own
    prompt scroll off the top with no way to reach them.
    """
    quoted = shlex.quote(str(body))
    # The body is conversation text on disk. Deleting it on the way out of
    # every exit path — dismissed, timed out, or the client went away —
    # keeps it from outliving the popup that was supposed to consume it.
    trap = f"trap 'rm -f {quoted}' EXIT INT TERM HUP; "
    if hold:
        # `less` is both the pager and the dismissal (`q`). Without it, `dd`
        # reads a single raw byte so any key ends the popup — Enter-only would
        # swallow the whole first line the user typed. `stty` failing on a
        # detached tty must not abort the script, hence the guards.
        script = trap + (
            f"if command -v less >/dev/null 2>&1; then less -R {quoted}; "
            f"else cat {quoted}; printf '\\n[any key] '; "
            "stty raw -echo 2>/dev/null || true; "
            "dd bs=1 count=1 >/dev/null 2>&1 || true; fi"
        )
    else:
        script = trap + f"cat {quoted}; sleep {NOTICE_SECONDS}"
    # No `=` exact-match prefix: that syntax is for session targets, and tmux
    # parses `-t` here as a pane target, which rejects it outright.
    return [
        "display-popup", "-E", "-w", width, "-h", height,
        "-t", session_name,
        "timeout", str(POPUP_TIMEOUT_SECONDS), "sh", "-c", script,
    ]
