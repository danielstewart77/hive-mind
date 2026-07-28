"""A finger drag has to reach tmux's history, and only `mouse on` lets it.

The browser tile has no wheel, so it synthesises SGR (1006) wheel reports
from a touch drag and writes them into the attached client's pty. Where
those bytes land is decided entirely by tmux's `mouse` option, and the
default is off: tmux drops them, the pane's app never enabled mouse
reporting for itself, and the drag scrolls nothing at all. That is the whole
reason the scrollback behind a session was unreachable from a phone --
nothing in the tile was broken, the bytes had nowhere to go.

The second test proves it against a real tmux rather than an option list,
because the claim is about tmux's behaviour, not about our argv. `send-keys`
would prove nothing: it injects into the pane and never passes tmux's own
input parser, which is the layer the `mouse` option lives in.
"""

import os
import pty
import shutil
import signal
import subprocess
import time

import pytest

from minds import pty_attach

MOUSE_ON = ["set", "-g", "mouse", "on"]


def test_the_terminal_turns_the_mouse_on():
    assert MOUSE_ON in pty_attach._TMUX_OPTIONS


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_a_wheel_report_scrolls_history_under_the_shipped_options(tmp_path):
    sock = str(tmp_path / "scrollback.sock")

    def tmux(*args):
        return subprocess.run(
            ["tmux", "-S", sock, *args], capture_output=True, text=True
        ).stdout.strip()

    def pane_mode_after_three_notches():
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child never returns
            os.environ["TERM"] = "xterm-256color"
            os.execvp("tmux", ["tmux", "-S", sock, "attach-session", "-d", "-t", "=t"])
        try:
            time.sleep(1.0)
            for _ in range(3):
                os.write(fd, b"\x1b[<64;10;5M")  # wheel-up, SGR 1006
                time.sleep(0.25)
            time.sleep(0.5)
            return tmux("list-panes", "-a", "-F", "#{pane_in_mode}")
        finally:
            os.kill(pid, signal.SIGTERM)
            os.close(fd)

    try:
        subprocess.run(
            ["tmux", "-S", sock, "-f", "/dev/null", "new-session", "-d", "-s", "t",
             "-x", "80", "-y", "24", "sh -c 'seq 1 500; sleep 30'"],
            capture_output=True,
        )
        for option in pty_attach._TMUX_OPTIONS:
            if option[2] in ("default-terminal", "terminal-features"):
                continue  # needs a real terminfo entry; irrelevant here
            tmux(*option)
        time.sleep(0.5)
        assert pane_mode_after_three_notches() == "1", (
            "the wheel report did not put the pane into copy-mode, so the "
            "history is still unreachable from a phone"
        )
    finally:
        subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)
