"""The terminal against a real tmux, with a real app that never redraws.

This is the test the whole browser-terminal saga was missing. Everything
that went wrong — a tile that opened black, a rotation that left ribboned
half-lines, a reattach that showed the question and nothing after it — was
the same missing capability: a bare pty gives a byte stream and a winsize
and no way to ask what is on screen or to ask for a redraw, so an app that
ignores SIGWINCH leaves the viewer holding whatever it last happened to
emit.

The stand-in app below is deliberately the worst case and the real one: it
paints once on the alternate screen and then ignores SIGWINCH forever, which
is what the harness CLIs were measured doing. If tmux repaints *that* at a
geometry it has never drawn for, nothing the browser does can strand a tile.
"""

import fcntl
import os
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path

import pytest

from minds.pty_attach import TmuxTerminals, seeded_pane_command

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

# Paints three markers on the alternate screen, then ignores every resize.
_STUBBORN_APP = """
import signal, sys, time
signal.signal(signal.SIGWINCH, signal.SIG_IGN)
sys.stdout.write("\\x1b[?1049h\\x1b[H\\x1b[2J")
sys.stdout.write("\\x1b[1;1HHEADER-ALPHA\\r\\n")
sys.stdout.write("\\x1b[3;1H\\x1b[32mgreen-BETA\\x1b[0m\\r\\n")
sys.stdout.write("\\x1b[10;1H> prompt-GAMMA")
sys.stdout.flush()
while True:
    time.sleep(3600)
"""
_MARKERS = ("HEADER-ALPHA", "green-BETA", "prompt-GAMMA")


def _read_for(fd: int, seconds: float) -> bytes:
    out = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.2)
        if not readable:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return out


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@pytest.fixture
def terminal(tmp_path):
    """A real tmux session running the stubborn app, on a socket of our own."""
    terminals = TmuxTerminals(f"testmind-{uuid.uuid4().hex[:8]}", Path("/tmp"))
    app = tmp_path / "stubborn_app.py"
    app.write_text(_STUBBORN_APP)
    argv = [sys.executable, str(app)]

    session_id = f"test-{uuid.uuid4().hex[:8]}"
    clients: list = []

    def attach(cols: int, rows: int):
        terminals.start(session_id, argv, env_overrides={}, cols=cols, rows=rows)
        proc, master_fd = terminals.attach(
            session_id, env_overrides={}, cols=cols, rows=rows)
        clients.append((proc, master_fd))
        return proc, master_fd

    def detach(proc, master_fd) -> None:
        proc.terminate()
        proc.wait(timeout=5)
        os.close(master_fd)

    yield terminals, session_id, attach, detach

    for proc, master_fd in clients:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        try:
            os.close(master_fd)
        except OSError:
            pass
    subprocess.run(["tmux", "-L", terminals.socket, "kill-server"], capture_output=True)
    # kill-server ends the process; the socket file it was listening on is
    # left behind, and a test run leaves one per test.
    try:
        os.unlink(f"/tmp/tmux-{os.getuid()}/{terminals.socket}")
    except OSError:
        pass


def test_attaching_paints_the_current_screen(terminal):
    _, _, attach, detach = terminal
    proc, fd = attach(100, 30)
    try:
        painted = _read_for(fd, 3.0).decode("utf8", "replace")
        for marker in _MARKERS:
            assert marker in painted
    finally:
        detach(proc, fd)


def test_reattaching_at_a_new_width_repaints_the_whole_screen(terminal):
    # The phone-after-desktop case. The app has emitted nothing since the
    # first paint and will not redraw, so every byte here comes from tmux's
    # own screen model, drawn for a width it has never seen before.
    _, _, attach, detach = terminal
    proc, fd = attach(100, 30)
    _read_for(fd, 2.0)
    detach(proc, fd)

    proc, fd = attach(44, 20)
    try:
        painted = _read_for(fd, 3.0).decode("utf8", "replace")
        for marker in _MARKERS:
            assert marker in painted
    finally:
        detach(proc, fd)


def test_resizing_a_live_client_repaints_it(terminal):
    # A phone rotating mid-session: no keystroke, no output from the app, and
    # the tile still has to end up holding a screen drawn for its new
    # geometry. Without a controlling tty the SIGWINCH reaches nobody.
    _, _, attach, detach = terminal
    proc, fd = attach(100, 30)
    try:
        _read_for(fd, 2.0)

        _set_winsize(fd, 44, 20)
        repaint = _read_for(fd, 2.0).decode("utf8", "replace")

        assert repaint, "a resize produced no output at all"
        for marker in _MARKERS:
            assert marker in repaint
    finally:
        detach(proc, fd)


def test_colour_survives_the_repaint(terminal):
    # Repainting the characters alone hands back a monochrome ghost of a
    # heavily coloured TUI.
    _, _, attach, detach = terminal
    proc, fd = attach(100, 30)
    _read_for(fd, 2.0)
    detach(proc, fd)

    proc, fd = attach(60, 20)
    try:
        painted = _read_for(fd, 3.0).decode("utf8", "replace")
        assert "\x1b[32m" in painted
    finally:
        detach(proc, fd)


def test_the_conversation_outlives_the_viewer(terminal):
    # Closing a tab must not end the turn. The client dies; the app does not.
    terminals, session_id, attach, detach = terminal
    proc, fd = attach(100, 30)
    _read_for(fd, 2.0)
    detach(proc, fd)

    assert terminals.alive(session_id) is True


def test_kill_ends_the_conversation(terminal):
    terminals, session_id, attach, detach = terminal
    proc, fd = attach(100, 30)
    _read_for(fd, 1.0)
    detach(proc, fd)

    assert terminals.kill(session_id) is True
    assert terminals.alive(session_id) is False


def test_a_session_name_that_prefixes_another_answers_only_for_itself(terminal):
    # tmux targets are prefix-matched by default, so without the `=`
    # exact-match form one session's id answers for another's pane.
    terminals, session_id, attach, detach = terminal
    proc, fd = attach(80, 24)
    try:
        assert terminals.alive(session_id) is True
        assert terminals.alive(session_id[:-2]) is False
    finally:
        detach(proc, fd)


def test_rotation_keeps_the_session_the_pane_and_the_client(terminal, tmp_path):
    # The whole point of rotating in place: the conversation is replaced, the
    # session and the terminal are not. The tmux session keeps its name, the
    # attached client is still the same process holding the same pty, and the
    # new conversation's output arrives on it without anything reattaching.
    successor = tmp_path / "successor_app.py"
    successor.write_text(
        'import sys, time\n'
        'sys.stdout.write("SUCCESSOR-DELTA\\r\\n"); sys.stdout.flush()\n'
        'while True: time.sleep(3600)\n'
    )

    terminals, session_id, attach, detach = terminal
    proc, fd = attach(100, 30)
    try:
        assert _read_for(fd, 3.0), "the tile never got its first paint"

        # A real carry-forward is a composed prompt, not a phrase: soul,
        # recent memory, the summary, the turns typed during the window.
        # Passed in the command, this is what tmux rejects outright with
        # "command too long" — the rotation then never happens at all.
        argv = seeded_pane_command(
            [sys.executable, str(successor)], "carry-forward " * 4000,
            tmp_path / "seed.txt",
        )
        terminals.respawn(session_id, argv, env_overrides={})

        assert proc.poll() is None, "the attached client died during the rotation"
        # Same session, still alive under the same id — that id is the
        # conversation's permanent identity everything above tmux is keyed to.
        assert terminals.alive(session_id) is True
        after = _read_for(fd, 4.0).decode("utf8", "replace")
        assert "SUCCESSOR-DELTA" in after
    finally:
        detach(proc, fd)


def test_a_second_attach_takes_the_keyboard_from_the_first(terminal):
    # One conversation, one keyboard: the older client is detached by tmux
    # rather than left typing into the same pane.
    _, _, attach, detach = terminal
    first_proc, first_fd = attach(100, 30)
    _read_for(first_fd, 2.0)

    second_proc, second_fd = attach(80, 24)
    try:
        assert _read_for(second_fd, 3.0), "the new client got no paint"
        first_proc.wait(timeout=5)  # detached by `attach-session -d`
        assert first_proc.poll() is not None
    finally:
        detach(second_proc, second_fd)
        try:
            os.close(first_fd)
        except OSError:
            pass
