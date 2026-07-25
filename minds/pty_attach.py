"""Browser-terminal attach for containerized minds.

Gives a mind the WebSocket route the web terminal reverse-proxies into:
``/sessions/{session_id}/attach-pty``. A mind without this route answers the
handshake with 403 and the terminal has nothing to render, so this module is
what the difference between "has a terminal" and "doesn't" comes down to.

The terminal is owned by the **session**, not by the socket. One
``_PtyHandle`` per session id holds the process, the pty master fd and a
scrollback ring; attaching adopts the session's existing terminal and
replays that ring. A turn in flight therefore survives a closed tab, a
locked phone or a dropped connection, and a second attach to the same
session evicts the older socket instead of spawning a rival harness process
on one conversation. The process ends only on an explicit teardown or via
the idle reaper.

The pty plumbing here is harness-agnostic: each mind passes a ``spawn``
callable that knows how to put *its* CLI under a pty, and raises
``PtyUnavailable`` when this session has nothing to attach to. That is what
lets a Claude mind and a Codex mind share one implementation despite
disagreeing about who mints a conversation id.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import struct
import subprocess
import termios
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

log = logging.getLogger("hive-mind.minds.pty")

_PTY_CHUNK = 65536
_PTY_MIN_COLS, _PTY_MAX_COLS = 20, 500
_PTY_MIN_ROWS, _PTY_MAX_ROWS = 5, 200

# How much terminal output to retain per session so a reattach can repaint
# what happened while nobody was watching. A turn's worth of streamed
# reasoning fits comfortably; older bytes fall off the front.
_PTY_SCROLLBACK_BYTES = 512 * 1024
# An unattached pty is kept alive this long so switching browser tiles,
# locking a phone, or losing wifi does not destroy an in-flight turn.
_PTY_IDLE_TIMEOUT_S = float(os.environ.get("PTY_IDLE_TIMEOUT_SECONDS", "3600"))
_PTY_REAP_INTERVAL_S = 60.0

# Queue sentinels for the per-attachment output pump.
_PTY_EOF = object()       # the harness process exited
_PTY_EVICTED = object()   # a newer attachment took this session over


class PtyUnavailable(Exception):
    """This session cannot be attached to, and retrying will not help.

    Raised by a mind's spawn callable — e.g. a Codex mind whose conversation
    has not taken its first turn yet, so no thread exists to resume and
    starting one would fork a second conversation behind the session's back.
    """


def clamp_winsize(cols: int, rows: int) -> tuple[int, int]:
    return (
        max(_PTY_MIN_COLS, min(_PTY_MAX_COLS, cols)),
        max(_PTY_MIN_ROWS, min(_PTY_MAX_ROWS, rows)),
    )


def set_winsize(fd: int, cols: int, rows: int) -> None:
    """Set the pty's window size; the kernel delivers SIGWINCH to the TUI."""
    cols, rows = clamp_winsize(cols, rows)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


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


def open_pty_process(
    cmd: list[str], *, env: dict[str, str], cwd: str, cols: int, rows: int
) -> tuple[subprocess.Popen, int]:
    """Run ``cmd`` under a new pty sized to the browser tile.

    Sizing before the process starts means the TUI's first paint already
    matches the tile, rather than rendering for 80x24 and reflowing.
    """
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        ensure_tui_first_run_flags(Path(config_dir), cwd)
    master_fd, slave_fd = pty.openpty()
    cols, rows = clamp_winsize(cols, rows)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        cwd=cwd,
        preexec_fn=os.setsid,
        close_fds=True,
    )
    os.close(slave_fd)
    return proc, master_fd


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


class _PtyHandle:
    """A live harness TUI, owned by a session rather than by a socket."""

    __slots__ = ("session_id", "proc", "master_fd", "conversation_id", "cols", "rows",
                 "scrollback", "queue", "detached_at", "alive", "loop")

    def __init__(self, session_id: str, proc, master_fd: int, conversation_id: str | None,
                 cols: int = 80, rows: int = 24):
        self.session_id = session_id
        self.proc = proc
        self.master_fd = master_fd
        self.conversation_id = conversation_id
        # The geometry the scrollback below was captured at — see resize().
        self.cols, self.rows = clamp_winsize(cols, rows)
        self.scrollback = bytearray()
        self.queue: asyncio.Queue | None = None  # set while a socket is attached
        self.detached_at: float | None = time.time()
        self.alive = True
        self.loop: asyncio.AbstractEventLoop | None = None

    def resize(self, cols: int, rows: int) -> None:
        """Retarget the pty, dropping scrollback that can no longer replay.

        Captured terminal output carries hard line breaks at the width it
        was emitted for. Replaying an 80-column capture into a 44-column
        tile doesn't reflow it, it shreds it — the ragged half-lines and
        orphaned trailing characters a phone shows after a session was
        last driven from a desktop. Once the width changes the capture has
        stopped being replayable, so it's discarded and the TUI's SIGWINCH
        repaint becomes the only source of truth for the new geometry.
        A height-only change wraps identically, so it keeps its history.
        """
        cols, rows = clamp_winsize(cols, rows)
        if cols != self.cols:
            self.scrollback.clear()
        self.cols, self.rows = cols, rows
        set_winsize(self.master_fd, cols, rows)

    def feed(self, data: bytes) -> None:
        self.scrollback += data
        if len(self.scrollback) > _PTY_SCROLLBACK_BYTES:
            del self.scrollback[:len(self.scrollback) - _PTY_SCROLLBACK_BYTES]
        if self.queue is not None:
            self.queue.put_nowait(data)

    def signal(self, sentinel: object) -> None:
        if self.queue is not None:
            self.queue.put_nowait(sentinel)


PTYS: dict[str, _PtyHandle] = {}


def _register_reader(handle: _PtyHandle) -> None:
    """Drain the pty into the handle for the process's whole life.

    Registered per event loop rather than per attachment — that is what lets
    output keep flowing into the scrollback while nobody is watching.
    """
    loop = asyncio.get_event_loop()
    if handle.loop is loop:
        return
    handle.loop = loop

    def _on_readable() -> None:
        try:
            data = os.read(handle.master_fd, _PTY_CHUNK)
        except OSError:
            data = b""
        if not data:
            handle.alive = False
            try:
                loop.remove_reader(handle.master_fd)
            except (ValueError, OSError):
                pass
            handle.signal(_PTY_EOF)
            return
        handle.feed(data)

    loop.add_reader(handle.master_fd, _on_readable)


def teardown(session_id: str) -> bool:
    """Kill a session's terminal process and forget it. True if one existed.

    Minds call this from their session-delete route: the terminal is a
    separate process from the tracked stream subprocess, so killing the
    session has to reach both or the TUI outlives its conversation.
    """
    handle = PTYS.pop(session_id, None)
    if handle is None:
        return False
    handle.alive = False
    handle.signal(_PTY_EOF)
    if handle.loop is not None and not handle.loop.is_closed():
        try:
            handle.loop.remove_reader(handle.master_fd)
        except (ValueError, OSError, RuntimeError):
            pass
    try:
        os.close(handle.master_fd)
    except OSError:
        pass
    proc = handle.proc
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    log.info("Tore down pty for session %s (pid=%s)", session_id, proc.pid)
    return True


async def reap_idle() -> None:
    """Kill terminals whose process exited, or that nobody came back to."""
    while True:
        await asyncio.sleep(_PTY_REAP_INTERVAL_S)
        now = time.time()
        for session_id, handle in list(PTYS.items()):
            if handle.proc.poll() is not None or not handle.alive:
                teardown(session_id)
            elif (handle.queue is None and handle.detached_at is not None
                    and now - handle.detached_at > _PTY_IDLE_TIMEOUT_S):
                log.info("Reaping pty for session %s — unattached for %.0fs",
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


def _open_session_pty(session_id, model, conversation_id, cols, rows, spawn) -> _PtyHandle:
    """Return the session's live terminal, spawning one only if needed."""
    handle = PTYS.get(session_id)
    if handle is not None:
        if handle.alive and handle.proc.poll() is None:
            _register_reader(handle)
            return handle
        teardown(session_id)  # dead process still on the books

    proc, master_fd = spawn(
        session_id=session_id, model=model, conversation_id=conversation_id,
        cols=cols, rows=rows,
    )
    handle = _PtyHandle(session_id, proc, master_fd, conversation_id, cols, rows)
    PTYS[session_id] = handle
    _register_reader(handle)
    return handle


def install_pty_attach(
    app: FastAPI,
    *,
    mind_name: str,
    spawn: Callable[..., tuple[subprocess.Popen, int]],
) -> None:
    """Mount ``/sessions/{session_id}/attach-pty`` on a mind's app.

    ``spawn`` is called as
    ``spawn(session_id=, model=, conversation_id=, cols=, rows=)`` and returns
    ``(Popen, master_fd)``; raising :class:`PtyUnavailable` refuses the attach
    with the reason instead of opening a terminal on nothing.
    """

    @app.on_event("startup")
    async def _start_pty_reaper() -> None:  # pragma: no cover - lifecycle glue
        asyncio.ensure_future(reap_idle())

    @app.websocket("/sessions/{session_id}/attach-pty")
    async def attach_pty(
        websocket: WebSocket,
        session_id: str,
        resume_sid: str | None = None,
        model: str = "sonnet",
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        await websocket.accept()

        # Attaching means attaching. Without the session's conversation id
        # there is nothing to attach TO, and opening a terminal anyway is how
        # a live session came up blank — so refuse and let the caller show it.
        if not (resume_sid or "").strip():
            log.warning("attach-pty for session %s carried no conversation id", session_id)
            await websocket.close(code=1008, reason="no conversation id for this session")
            return

        cols, rows = clamp_winsize(cols, rows)
        try:
            handle = _open_session_pty(session_id, model, resume_sid, cols, rows, spawn)
        except PtyUnavailable as exc:
            log.info("attach-pty refused for %s session %s: %s", mind_name, session_id, exc)
            await websocket.close(code=1008, reason=str(exc)[:120])
            return
        except Exception:
            log.exception("Failed to spawn pty for session %s", session_id)
            await websocket.close(code=1011, reason="failed to start terminal")
            return

        # A second attach to the same session takes over; the stale socket is
        # told to go away rather than both fighting over one keyboard.
        handle.signal(_PTY_EVICTED)
        queue: asyncio.Queue = asyncio.Queue()
        handle.queue = queue
        handle.detached_at = None

        # Resize before snapshotting: a tile of a different width invalidates
        # the capture, so the replay must be taken after that has been settled.
        try:
            handle.resize(cols, rows)
        except OSError:
            pass
        backlog = bytes(handle.scrollback)

        async def pump_to_ws() -> None:
            if backlog:
                await websocket.send_bytes(backlog)
            while True:
                item = await queue.get()
                if item is _PTY_EOF:
                    await websocket.close(code=1000, reason="terminal exited")
                    return
                if item is _PTY_EVICTED:
                    await websocket.close(code=1012, reason="attached elsewhere")
                    return
                await websocket.send_bytes(item)

        pump_out_task = asyncio.ensure_future(pump_to_ws())
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
                if data:
                    os.write(handle.master_fd, data)
        except (WebSocketDisconnect, OSError):
            pass
        finally:
            pump_out_task.cancel()
            # Detach only — the terminal keeps running so the turn in flight
            # survives and the next attach can see how it finished.
            if handle.queue is queue:
                handle.queue = None
                handle.detached_at = time.time()
            log.info("Detached from pty for session %s (pid=%s, still running=%s)",
                     session_id, handle.proc.pid, handle.proc.poll() is None)
