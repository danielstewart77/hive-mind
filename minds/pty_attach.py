"""Browser-terminal attach for containerized minds, tmux-backed.

Gives a mind the WebSocket route the web terminal reverse-proxies into,
``/sessions/{session_id}/attach-pty``, plus the ``rotate-pty`` route
hive-comms calls when a terminal conversation crosses its context
threshold. A mind without these answers the handshake with 403 and the
terminal has nothing to render.

**The conversation lives in tmux; a tile is a client.** Each hive session
owns a tmux session on a dedicated socket, and the harness CLI runs inside
it. Attaching starts a ``tmux attach-session -d`` client in a pty of the
tile's geometry; ending that client detaches the view without touching the
conversation, and re-attaching joins the same tmux session rather than
starting a rival CLI process. tmux owns the screen model and the history: it
repaints on attach and on live resize, which is why there is no scrollback
ring, no VT emulator and no snapshot painter here. A turn in flight survives
a closed tab, a locked phone or a dropped connection; the process ends only
on an explicit teardown or via the idle reaper.

The tmux plumbing is harness-agnostic — :class:`TmuxTerminals` knows how to
start, attach to, respawn and kill a session's terminal, and each mind
passes the argv its own CLI needs. That is what lets a Claude mind and a
Codex mind share one implementation despite disagreeing about who mints a
conversation id.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import shlex
import struct
import subprocess
import termios
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

log = logging.getLogger("hive-mind.minds.pty")

_PTY_CHUNK = 65536
_PTY_MIN_COLS, _PTY_MAX_COLS = 20, 500
_PTY_MIN_ROWS, _PTY_MAX_ROWS = 5, 200

# An unattached terminal is kept alive this long so switching browser tiles,
# locking a phone, or losing wifi does not destroy an in-flight turn.
_PTY_IDLE_TIMEOUT_S = float(os.environ.get("PTY_IDLE_TIMEOUT_SECONDS", "3600"))
_PTY_REAP_INTERVAL_S = 60.0

# How often to send a keepalive byte to an attached socket. A mobile
# rotation or a network blip leaves the TCP connection half-open — the
# browser keeps reporting it OPEN and doesn't fire onclose for ~30s — so the
# tile can't tell a dead socket from a merely idle one. A NUL on a fixed
# cadence (xterm ignores it) gives the browser a heartbeat to miss.
_PTY_KEEPALIVE_S = 5.0
_PTY_KEEPALIVE_BYTE = b"\x00"

# Queue sentinels for the per-attachment output pump.
_PTY_EOF = object()       # the terminal's client ended
_PTY_EVICTED = object()   # a newer attachment took this session over

# What the browser is: xterm.js, which reads xterm-256color and renders
# 24-bit colour. The pane's own TERM is tmux-256color, set below.
_CLIENT_TERM = "xterm-256color"

# Applied ahead of every session creation, so they hold from the pane's first
# byte. `history-limit` and `default-terminal` are read when the pane is
# created and can't be retrofitted; `exit-empty off` keeps the server up
# between the last session ending and the next one starting; `prefix None`
# and `escape-time 0` keep tmux's own key handling out of a TUI that wants
# every keystroke, Escape most of all; `status off` gives the pane the row
# the status bar would take; `window-size latest` sizes the window to the
# client that most recently attached, which is the only client we allow.
_TMUX_OPTIONS = [
    ["set", "-g", "exit-empty", "off"],
    ["set", "-g", "status", "off"],
    ["set", "-g", "prefix", "None"],
    ["set", "-g", "escape-time", "0"],
    ["set", "-g", "history-limit", "50000"],
    ["set", "-g", "window-size", "latest"],
    ["set", "-g", "destroy-unattached", "off"],
    ["set", "-g", "default-terminal", "tmux-256color"],
    ["set", "-gas", "terminal-features", ",xterm-256color:RGB"],
]

# A single argv entry cannot exceed MAX_ARG_STRLEN (32 pages, 128 KiB on
# Linux); exec fails outright above it. A rotation seed reaches the harness
# as one argument however it is delivered, so it is capped short of that.
MAX_SEED_CHARS = 120_000


class PtyUnavailable(Exception):
    """This session cannot be attached to, and retrying will not help."""


def clamp_winsize(cols: int, rows: int) -> tuple[int, int]:
    return (
        max(_PTY_MIN_COLS, min(_PTY_MAX_COLS, cols)),
        max(_PTY_MIN_ROWS, min(_PTY_MAX_ROWS, rows)),
    )


def set_winsize(fd: int, cols: int, rows: int) -> None:
    """Set the pty's window size; the kernel delivers SIGWINCH to the client.

    tmux answers that signal by resizing the pane and repainting it from its
    own screen model, so the tile gets the current screen at its new
    geometry whether or not the app inside ever reacts to the resize.
    """
    cols, rows = clamp_winsize(cols, rows)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _take_controlling_tty() -> None:
    """Make the pty the child's controlling terminal, in the child.

    `setsid` alone leaves the process with no controlling terminal at all,
    and the kernel sends SIGWINCH to a terminal's foreground process group —
    of which there is none. That is why a resize used to reach the pty and
    stop there: the winsize changed, nothing was ever signalled, and the app
    on the far end went on rendering for the size it started at. The session
    leader has to claim the tty for the signal to have somewhere to go.
    stdin is the pty slave by the time this runs.
    """
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _merge_json_file(path: Path, mutate: Callable[[dict], bool]) -> None:
    """Apply ``mutate`` to ``path``'s JSON object, writing only if it changed.

    Atomic replace: a live harness process writes these files too, and a
    half-written config is a worse failure than the prompt being seeded away.
    """
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        log.warning("Could not read %s to seed TUI first-run state", path)
        return
    if not isinstance(data, dict) or not mutate(data):
        return

    tmp = path.with_name(path.name + ".pty-tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
        log.info("Seeded TUI first-run state in %s", path)
    except OSError:
        log.warning("Could not write %s to seed TUI first-run state", path)
        tmp.unlink(missing_ok=True)


def ensure_tui_first_run_flags(config_dir: Path, project_dir: str) -> None:
    """Pre-answer the interactive TUI's first-run gates for ``project_dir``.

    ``claude -p`` (the Telegram path) skips these gates outright, so a mind
    authenticated by ``CLAUDE_CODE_OAUTH_TOKEN`` converses fine while its
    config dir has never answered any of them. The TUI does *not* skip them,
    and a browser terminal shows whichever one it stops on as the entire
    session — the theme-then-login wizard reads as "it's asking me to log
    into my Anthropic account" even though auth is fine. All three are
    first-run state, not credentials:

    * ``hasCompletedOnboarding`` — the theme/login wizard;
    * ``hasTrustDialogAccepted`` — "is this a project you trust?", for the
      workspace the mind runs in (its own image's code, trusted by
      construction);
    * ``skipDangerousModePermissionPrompt`` — the bypass-permissions
      acceptance, which every mind spawns with by design.

    None is usefully answerable from a web tile, so mark them done. Only
    writes when something is actually missing.
    """
    def _seed_state(data: dict) -> bool:
        changed = data.get("hasCompletedOnboarding") is not True
        data["hasCompletedOnboarding"] = True
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            return changed
        entry = projects.setdefault(project_dir, {})
        if not isinstance(entry, dict):
            return changed
        for key in ("hasTrustDialogAccepted", "hasCompletedProjectOnboarding"):
            changed = changed or entry.get(key) is not True
            entry[key] = True
        return changed

    def _seed_settings(data: dict) -> bool:
        if data.get("skipDangerousModePermissionPrompt") is True:
            return False
        data["skipDangerousModePermissionPrompt"] = True
        return True

    _merge_json_file(config_dir / ".claude.json", _seed_state)
    _merge_json_file(config_dir / "settings.json", _seed_settings)


def claude_transcript_exists(conversation_id: str, project_dir: Path) -> bool:
    """True if the claude CLI already has a transcript for this id.

    Claude Code stores conversations at
    ``<config>/projects/<slugified-cwd>/<id>.jsonl`` where the slug is the
    cwd path with every ``/``, ``_`` and ``.`` turned into ``-``.
    """
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    slug = str(project_dir).replace("/", "-").replace("_", "-").replace(".", "-")
    return (config_dir / "projects" / slug / f"{conversation_id}.jsonl").exists()


def claude_conversation_flags(conversation_id: str, project_dir: Path) -> list[str]:
    """CLI flags binding a claude process to one conversation id.

    A transcript on disk means the conversation has spoken before, so resume
    it in place. No transcript means this is its first process, so declare
    the id. Either way the id is the gateway's, never the harness's — the
    two branches are the same conversation at different ages, not "existing"
    and "new". Passing ``--resume`` for an id with no transcript is how a
    session's first turn used to die with "No conversation found".
    """
    if claude_transcript_exists(conversation_id, project_dir):
        return ["--resume", conversation_id]
    return ["--session-id", conversation_id]


def capped_seed(system_prompt: str) -> str:
    """Trim an oversized carry-forward to what exec can actually carry.

    The tail is what survives: composition puts the rotation summary and the
    turns typed during the window last, and those are the ones the successor
    has to pick the conversation up from.
    """
    if len(system_prompt) <= MAX_SEED_CHARS:
        return system_prompt
    log.warning("Rotation seed of %d chars exceeds the %d-char exec limit — "
                "keeping the tail", len(system_prompt), MAX_SEED_CHARS)
    notice = ("[earlier context omitted: the carry-forward was too large to "
              "pass to the harness]\n\n")
    # The notice counts against the same argv entry the seed rides in, so the
    # tail is trimmed to leave room for it rather than added on top.
    return notice + system_prompt[-(MAX_SEED_CHARS - len(notice)):]


def seeded_pane_command(
    argv: list[str], system_prompt: str, seed_file: Path, *, seed_flag: str = "",
) -> list[str]:
    """Hand the pane its carry-forward without putting it in the command.

    A rotation seed is a composed prompt — soul, recent memory, the summary,
    the turns typed during the window — and tmux rejects a ``respawn-pane``
    whose command exceeds its own length limit with "command too long"; a
    single argv entry is capped at ``MAX_ARG_STRLEN`` regardless. So the seed
    goes to a file and the pane reads it back through a one-line shell that
    then ``exec``s the harness with it: the tmux command stays short however
    much context the successor is carrying.

    ``seed_flag`` is how this harness takes an opening context — claude has
    ``--append-system-prompt``; codex has no such flag and takes it as the
    positional opening turn, so the flag is empty there.
    """
    if not system_prompt:
        return argv

    system_prompt = capped_seed(system_prompt)
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(system_prompt)
    quoted_seed = shlex.quote(str(seed_file))
    harness = " ".join(shlex.quote(arg) for arg in argv)
    flag = f"{seed_flag} " if seed_flag else ""
    # Read then delete: the seed is one process's opening context, and it is
    # the whole conversation's memory sitting in a world-readable file.
    return [
        "/bin/sh", "-c",
        f'seed=$(cat {quoted_seed}); rm -f {quoted_seed}; '
        f'exec {harness} {flag}"$seed"',
    ]


class TmuxTerminals:
    """The tmux server holding one mind's browser terminals.

    A dedicated socket per mind: this server is ours, so its options and its
    lifetime can't be disturbed by a tmux the operator runs by hand inside
    the same container.
    """

    def __init__(self, mind_name: str, project_dir: Path):
        self.mind_name = mind_name
        self.project_dir = project_dir
        self.socket = f"{mind_name}-terminal"

    def session_name(self, session_id: str) -> str:
        """The tmux session that holds this hive session's terminal."""
        return f"{self.mind_name}-{session_id}"

    def _tmux(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            capture_output=True, text=True, env=env,
        )

    def alive(self, session_id: str) -> bool:
        """True while this session's terminal process is still running.

        The ``=`` is exact-match: tmux targets are prefix-matched by default,
        so a session whose id is a prefix of another's would answer for it.
        """
        target = f"={self.session_name(session_id)}"
        return self._tmux("has-session", "-t", target).returncode == 0

    def kill(self, session_id: str) -> bool:
        """End the terminal for good. True if there was one to end."""
        name = self.session_name(session_id)
        if self._tmux("kill-session", "-t", f"={name}").returncode != 0:
            return False
        log.info("Killed tmux session %s", name)
        return True

    def start(
        self, session_id: str, argv: list[str], *,
        env_overrides: dict[str, str], cols: int, rows: int,
    ) -> None:
        """Start the session's terminal, detached, if it isn't running.

        ``env_overrides`` ride on ``-e`` per session: a pane inherits from
        the tmux *server*, which was started before this conversation existed
        and knows nothing about it. Without ``CLIENT_REF`` in particular the
        Stop hook's rotation check bails on every fire and a terminal
        conversation never rotates — it just grows until the harness's own
        compaction is the only thing left to intervene.
        """
        if self.alive(session_id):
            return
        name = self.session_name(session_id)
        args: list[str] = []
        for option in _TMUX_OPTIONS:
            args.extend([*option, ";"])
        args.extend(["new-session", "-d", "-s", name, "-c", str(self.project_dir),
                     "-x", str(cols), "-y", str(rows)])
        for key, value in env_overrides.items():
            args.extend(["-e", f"{key}={value}"])
        args.append("--")
        args.extend(argv)

        env = dict(os.environ, **env_overrides)
        result = self._tmux(*args, env=env)
        if result.returncode != 0:
            raise RuntimeError(
                f"tmux refused to start the terminal for session {session_id}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        log.info("Started tmux session %s", name)

    def attach(
        self, session_id: str, *, env_overrides: dict[str, str], cols: int, rows: int,
    ) -> tuple[subprocess.Popen, int]:
        """Run a tmux client for this session in a pty of the tile's geometry.

        ``-d`` detaches whoever held the session before, so one conversation
        always has exactly one keyboard. The caller owns both returned
        lifecycles; ending them detaches the view without touching the
        conversation.
        """
        name = self.session_name(session_id)
        master_fd, slave_fd = pty.openpty()
        cols, rows = clamp_winsize(cols, rows)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = dict(os.environ, **env_overrides, TERM=_CLIENT_TERM)
        proc = subprocess.Popen(
            ["tmux", "-L", self.socket, "attach-session", "-d", "-t", name],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd=str(self.project_dir),
            preexec_fn=_take_controlling_tty,
            close_fds=True,
        )
        os.close(slave_fd)
        log.info("Attached tmux client to %s for session %s (pid=%d, %dx%d)",
                 name, session_id, proc.pid, cols, rows)
        return proc, master_fd

    def respawn(
        self, session_id: str, argv: list[str], *, env_overrides: dict[str, str],
    ) -> None:
        """Replace the pane's process in place, leaving every client attached.

        ``respawn-pane -k`` swaps the process without touching the window,
        the session, or any attached client — which is what makes a rotation
        invisible to the browser tile above it. No ``=`` exact-match prefix
        here: that syntax is for session targets, and tmux parses a pane
        target differently.
        """
        args = ["respawn-pane", "-k", "-t", self.session_name(session_id),
                "-c", str(self.project_dir)]
        for key, value in env_overrides.items():
            args.extend(["-e", f"{key}={value}"])
        args.append("--")
        args.extend(argv)

        env = dict(os.environ, **env_overrides)
        result = self._tmux(*args, env=env)
        if result.returncode != 0:
            # The pane still holds the old conversation's process, which is
            # the safe failure: the user keeps typing, the context just
            # didn't turn over, and the next Stop hook fire tries again.
            raise RuntimeError(
                f"tmux refused to respawn the pane for session {session_id}: "
                f"{(result.stderr or result.stdout).strip()}"
            )


class _PtyHandle:
    """A session's terminal, plus whichever browser tile is watching it now.

    The terminal itself lives in a tmux session named for the hive session
    and outlives every viewer. What this holds is the *client*: a ``tmux
    attach`` process in a pty of the current tile's geometry. Detaching (tab
    switch, phone lock, dropped wifi) ends the client and leaves the harness
    running mid-turn; the next attach starts a fresh client, and tmux paints
    the whole screen into it at whatever size that tile happens to be.
    Nothing here has to remember the bytes: tmux keeps the screen and the
    scrollback, and is the one thing in this stack that can be *asked* what
    the terminal currently looks like.
    """

    __slots__ = ("session_id", "terminals", "tmux_name", "conversation_id", "proc",
                 "master_fd", "cols", "rows", "queue", "detached_at", "alive", "loop")

    def __init__(self, session_id: str, terminals: "TmuxTerminals",
                 conversation_id: str, cols: int = 80, rows: int = 24):
        self.session_id = session_id
        # The tmux server this session's terminal lives on. Held per handle
        # rather than looked up globally: one process hosts one mind, but a
        # test process imports several.
        self.terminals = terminals
        self.tmux_name = terminals.session_name(session_id)
        # The session's conversation id, minted by the gateway and handed
        # down. The mind records it for logging; it never chooses it.
        self.conversation_id = conversation_id
        self.cols, self.rows = clamp_winsize(cols, rows)
        self.proc: subprocess.Popen | None = None   # the attached tmux client
        self.master_fd: int | None = None
        self.queue: asyncio.Queue | None = None  # set while a socket is attached
        self.detached_at: float | None = time.time()
        self.alive = True
        self.loop: asyncio.AbstractEventLoop | None = None

    def resize(self, cols: int, rows: int) -> None:
        """Retarget the client's pty and let tmux redraw for the new size."""
        cols, rows = clamp_winsize(cols, rows)
        self.cols, self.rows = cols, rows
        if self.master_fd is not None:
            set_winsize(self.master_fd, cols, rows)

    def push(self, data: bytes) -> None:
        """Send bytes to the attached socket, if one is attached."""
        if data and self.queue is not None:
            self.queue.put_nowait(data)

    def signal(self, sentinel: object) -> None:
        if self.queue is not None:
            self.queue.put_nowait(sentinel)


PTYS: dict[str, _PtyHandle] = {}

# Set by install_pty_attach — teardown() and the reaper are module-level
# because minds call them from their own session routes.
_TERMINALS: TmuxTerminals | None = None


def _register_reader(handle: _PtyHandle) -> None:
    """Forward everything the attached client emits, for as long as it lives.

    Re-registered on every attach: the fd belongs to the client, and each
    attach brings a new one. EOF here means the client ended — either
    because we detached it or because the harness exited and took the tmux
    session with it — so the socket is told and the liveness question is
    left to tmux, which is the only thing that knows.
    """
    _unregister_reader(handle)
    if handle.master_fd is None:
        return
    loop = asyncio.get_event_loop()
    handle.loop = loop
    fd = handle.master_fd

    def _on_readable() -> None:
        try:
            data = os.read(fd, _PTY_CHUNK)
        except OSError:
            data = b""
        if not data:
            try:
                loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            handle.signal(_PTY_EOF)
            return
        handle.push(data)

    loop.add_reader(fd, _on_readable)


def _unregister_reader(handle: _PtyHandle) -> None:
    if handle.loop is not None and handle.master_fd is not None and not handle.loop.is_closed():
        try:
            handle.loop.remove_reader(handle.master_fd)
        except (ValueError, OSError, RuntimeError):
            pass


def _detach_client(handle: _PtyHandle) -> None:
    """End the viewing client, leaving the conversation running in tmux."""
    _unregister_reader(handle)
    if handle.master_fd is not None:
        try:
            os.close(handle.master_fd)
        except OSError:
            pass
        handle.master_fd = None
    proc, handle.proc = handle.proc, None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def teardown(session_id: str) -> bool:
    """End a session's terminal for good. True if one existed.

    Kills the tmux session, which is what actually owns the harness process —
    detaching the client alone would leave the conversation running with
    nobody able to reach it. Minds call this from their session-delete and
    release routes: the terminal is a separate process from the tracked
    stream subprocess, so killing the session has to reach both or the TUI
    outlives its conversation.
    """
    handle = PTYS.pop(session_id, None)
    terminals = handle.terminals if handle is not None else _TERMINALS
    killed = terminals.kill(session_id) if terminals is not None else False
    if handle is None:
        return killed
    handle.alive = False
    handle.signal(_PTY_EOF)
    _detach_client(handle)
    log.info("Tore down terminal for session %s (tmux=%s)", session_id, handle.tmux_name)
    return True


def push_overlay(session_id: str, data: bytes) -> bool:
    """Write bytes straight to a session's attached socket, if one is there.

    For turns that arrived on another surface: the interactive terminal only
    shows what its own harness process drew, so a Telegram turn would leave
    the tile silently out of sync with what was actually said. tmux owns the
    pane's contents and has no way to be told about bytes it didn't produce,
    so this is a live overlay for whoever is watching now, cleared by the
    next repaint. A no-op when nobody is attached, which is the common case.
    """
    handle = PTYS.get(session_id)
    if handle is None or handle.queue is None:
        return False
    handle.push(data)
    return True


def mirror_turn(
    session_id: str | None,
    *,
    mind_name: str,
    assistant_texts: list[str],
    user_text: str | None = None,
    surface: str = "chat",
) -> bool:
    """Render a turn that arrived on another surface into the live terminal.

    A turn delivered through the non-interactive path (Telegram today; any
    future non-terminal surface) never touches the terminal's harness
    process, so without this the tile silently drifts out of sync with what
    was actually said and heard.
    """
    if not assistant_texts or not session_id:
        return False
    parts = ["\r\n"]
    if user_text:
        parts.append(
            f"\x1b[36m[{surface}] user:\x1b[0m "
            + user_text.replace("\n", "\r\n") + "\r\n"
        )
    reply = "\n\n".join(assistant_texts).replace("\n", "\r\n")
    parts.append(f"\x1b[32m[{surface}] {mind_name}:\x1b[0m {reply}\r\n")
    return push_overlay(session_id, "".join(parts).encode())


async def reap_idle() -> None:
    """Kill terminals whose harness exited, or that nobody came back to."""
    while True:
        await asyncio.sleep(_PTY_REAP_INTERVAL_S)
        now = time.time()
        for session_id, handle in list(PTYS.items()):
            if not handle.terminals.alive(session_id):
                teardown(session_id)
            elif (handle.queue is None and handle.detached_at is not None
                    and now - handle.detached_at > _PTY_IDLE_TIMEOUT_S):
                log.info("Reaping terminal for session %s — unattached for %.0fs",
                         session_id, now - handle.detached_at)
                teardown(session_id)


def _control_frame(handle: _PtyHandle, text: str) -> bool:
    """Apply a TEXT control frame to the pty; True if it was one.

    The attach protocol is: BINARY frames are raw terminal bytes, TEXT
    frames are JSON control messages (currently only resize). Non-JSON text
    is not a control frame — the caller writes it to the pty instead.
    """
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(msg, dict) or msg.get("type") != "resize":
        return False
    try:
        handle.resize(int(msg["cols"]), int(msg["rows"]))
    except (KeyError, TypeError, ValueError, OSError):
        pass  # malformed or fd already closed — drop, never crash the pump
    return True


def _open_session_pty(session_id, spawn, terminals, **spawn_kwargs) -> _PtyHandle:
    """Attach this tile to the session's terminal, starting one if needed.

    The harness process is keyed by session and reused; only the viewing
    client is per-attach. A tile arriving while another holds the session
    evicts it first — one conversation, one keyboard — and the eviction is
    announced before the old client dies so the displaced tile is told it
    was replaced rather than that the terminal exited.
    """
    cols = spawn_kwargs["cols"]
    rows = spawn_kwargs["rows"]
    handle = PTYS.get(session_id)
    if handle is not None:
        handle.signal(_PTY_EVICTED)
        _detach_client(handle)
    else:
        handle = _PtyHandle(session_id, terminals,
                            spawn_kwargs.get("conversation_id") or "", cols, rows)
        PTYS[session_id] = handle

    proc, master_fd = spawn(session_id=session_id, **spawn_kwargs)
    handle.proc = proc
    handle.master_fd = master_fd
    handle.cols, handle.rows = clamp_winsize(cols, rows)
    handle.queue = None
    handle.alive = True
    _register_reader(handle)
    return handle


def install_pty_attach(
    app: FastAPI,
    *,
    mind_name: str,
    terminals: TmuxTerminals,
    spawn: Callable[..., tuple[subprocess.Popen, int]],
    rotate: Callable[..., bool] | None = None,
) -> None:
    """Mount the browser-terminal routes on a mind's app.

    ``spawn`` is called as ``spawn(session_id=, model=, conversation_id=,
    harness_sid=, cols=, rows=, client_ref=, owner_type=, owner_ref=)`` and
    returns ``(Popen, master_fd)`` for a tmux client on the session's
    terminal; raising :class:`PtyUnavailable` refuses the attach with the
    reason instead of opening a terminal on nothing.

    ``rotate`` is called as ``rotate(session_id=, new_claude_sid=, model=,
    system_prompt=, client_ref=, owner_type=, owner_ref=)`` and returns
    whether a live terminal was rotated in place.
    """
    global _TERMINALS
    _TERMINALS = terminals

    @app.on_event("startup")
    async def _start_pty_reaper() -> None:  # pragma: no cover - lifecycle glue
        asyncio.ensure_future(reap_idle())

    @app.post("/sessions/{session_id}/rotate-pty")
    async def rotate_pty(session_id: str, request: Request):
        """Turn a live terminal's conversation over without disturbing the tile.

        Rotation replaces the conversation; the session and the terminal
        stay. The pane's process is swapped for one on a fresh harness
        conversation, seeded with the carry-forward, while the tmux client —
        and so the pty, the socket, and the browser tile holding them — is
        left alone. The session id never changes, so nothing above this has
        to be told anything: the user keeps typing into the same pane, and
        only the context behind it turned over.

        Reports ``rotated: false`` when there is no live terminal here. That
        is the normal answer for a conversation nobody has a tile open on.
        """
        body = await request.json()
        new_claude_sid = (body.get("new_claude_sid") or "").strip()
        if not new_claude_sid:
            return JSONResponse({"error": "new_claude_sid required"}, status_code=400)

        handle = PTYS.get(session_id)
        if handle is None or rotate is None:
            return {"session_id": session_id, "rotated": False}

        try:
            rotated = await asyncio.to_thread(
                rotate,
                session_id=session_id,
                new_claude_sid=new_claude_sid,
                model=body.get("model") or "",
                system_prompt=body.get("system_prompt") or "",
                client_ref=body.get("client_ref"),
                owner_type=body.get("owner_type"),
                owner_ref=body.get("owner_ref"),
            )
        except Exception:
            log.exception("Rotation failed for %s session %s", mind_name, session_id)
            return JSONResponse({"error": "rotation failed"}, status_code=500)
        if not rotated:
            return {"session_id": session_id, "rotated": False}

        # The pty, its reader task and the attached queue all belong to a
        # tmux client that survived the swap, so the handle carries over
        # untouched — only the conversation it is pinned to changed.
        handle.conversation_id = new_claude_sid
        log.info("Rotated the conversation in session %s's terminal onto %s",
                 session_id, new_claude_sid)
        return {"session_id": session_id, "rotated": True, "claude_sid": new_claude_sid}

    @app.websocket("/sessions/{session_id}/attach-pty")
    async def attach_pty(
        websocket: WebSocket,
        session_id: str,
        resume_sid: str | None = None,
        harness_sid: str | None = None,
        model: str = "sonnet",
        cols: int = 80,
        rows: int = 24,
        client_ref: str | None = None,
        owner_type: str | None = None,
        owner_ref: str | None = None,
    ) -> None:
        """Bidirectional raw-byte bridge between a browser tile and this
        session's interactive harness CLI.

        The terminal is keyed by ``session_id`` and outlives the socket: it
        runs in tmux, so attaching starts a client on the *existing*
        conversation and tmux paints the current screen into it, rather than
        spawning a second harness process on the same transcript.

        ``cols``/``rows`` size the pty the client runs in; later TEXT frames
        carrying ``{"type":"resize","cols":N,"rows":M}`` retarget it live,
        and tmux redraws the pane for the new geometry.
        """
        await websocket.accept()

        # Attaching means attaching. Without the session's conversation id
        # there is nothing to attach TO, and opening a terminal anyway is how
        # a live session came up blank — so refuse and let the caller show it.
        if not (resume_sid or "").strip():
            log.warning("attach-pty for session %s carried no conversation id", session_id)
            await websocket.close(code=1008, reason="no conversation id for this session")
            return

        # hive-comms has no chat-id-like concept for a browser tile —
        # Telegram/Discord send a real client_ref, a terminal attach may not.
        # Without one, the rotation check's missing-client_ref guard bails on
        # every fire and the transcript just grows. The session id is already
        # unique and stable for this conversation, so it doubles as the key.
        client_ref = client_ref or session_id

        cols, rows = clamp_winsize(cols, rows)
        try:
            handle = _open_session_pty(
                session_id, spawn, terminals, model=model, conversation_id=resume_sid,
                harness_sid=harness_sid, cols=cols, rows=rows,
                client_ref=client_ref, owner_type=owner_type, owner_ref=owner_ref,
            )
        except PtyUnavailable as exc:
            log.info("attach-pty refused for %s session %s: %s", mind_name, session_id, exc)
            await websocket.close(code=1008, reason=str(exc)[:120])
            return
        except Exception:
            log.exception("Failed to open terminal for session %s", session_id)
            await websocket.close(code=1011, reason="failed to start terminal")
            return

        queue: asyncio.Queue = asyncio.Queue()
        handle.queue = queue
        handle.detached_at = None

        async def pump_to_ws() -> None:
            # No backlog to replay: the client was spawned at this tile's
            # geometry and tmux draws the whole screen into it on attach.
            while True:
                item = await queue.get()
                if item is _PTY_EOF:
                    await websocket.close(code=1000, reason="terminal exited")
                    return
                if item is _PTY_EVICTED:
                    await websocket.close(code=1012, reason="attached elsewhere")
                    return
                await websocket.send_bytes(item)

        async def keepalive() -> None:
            # A heartbeat the browser can miss: the NUL rides the same queue
            # as real output (so the pump stays the only sender) and xterm
            # ignores it. Its absence is how a half-open socket is detected
            # client-side long before the browser's own ~30s dead-connection
            # timeout would fire onclose.
            while True:
                await asyncio.sleep(_PTY_KEEPALIVE_S)
                queue.put_nowait(_PTY_KEEPALIVE_BYTE)

        pump_out_task = asyncio.ensure_future(pump_to_ws())
        keepalive_task = asyncio.ensure_future(keepalive())
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if data is None and msg.get("text") is not None:
                    if _control_frame(handle, msg["text"]):
                        continue
                    data = msg["text"].encode()
                if data and handle.master_fd is not None:
                    os.write(handle.master_fd, data)
        except (WebSocketDisconnect, OSError):
            pass
        finally:
            pump_out_task.cancel()
            keepalive_task.cancel()
            # Detach only — the harness keeps running inside tmux, so the
            # turn in flight survives and the next attach sees how it
            # finished. Clear the queue first so ending the client doesn't
            # look like an exit.
            if handle.queue is queue:
                handle.queue = None
                handle.detached_at = time.time()
                _detach_client(handle)
            log.info("Detached from terminal for session %s (tmux=%s, still running=%s)",
                     session_id, handle.tmux_name,
                     terminals.alive(session_id))
