"""A rotated terminal tells the user what happened to it.

``respawn-pane -k`` takes the pane's history with it, so before this the tile
went blank mid-conversation with nothing to say why — and neither harness can
be told to print the recap itself: ``claude`` runs on the alternate screen and
``codex`` paints over anything written ahead of its exec, while ``status off``
leaves the status line with nowhere to render. A tmux popup is drawn by the
*client*, over whatever the pane's app is doing, which is why the notices go
through one.

Six behaviours, one per requirement:

1. a rotation that has begun tells an attached terminal it is rotating
2. the terminal that comes back leads with "this session has been rotated"
3. the last exchange before the rotation is replayed under that line
4. a very long reply is trimmed to its last 50 lines
5. with no prior exchange, the rotation line stands alone
6. codex draws the same notices as claude

Each covers its requirement across both hops that implement it — the gateway
deciding to send, and the mind drawing — because a test that only checks the
rendering of a hand-fed dict passes while the thing that feeds it is broken.
"""

import asyncio
import contextlib
import fcntl
import os
import pty
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Each requirement here spans both trees — the gateway decides to send a
# notice, the mind draws it — and testing only one half is how a broken
# sender passes. `comms` comes from this suite's own pythonpath; `minds`
# lives at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from comms.models import ModelRegistry, Provider  # noqa: E402
from comms.sessions import ROTATING_NOTICE, SessionManager  # noqa: E402
from minds import pty_attach, pty_notice  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_ptys():
    pty_attach.PTYS.clear()
    yield
    for sid in list(pty_attach.PTYS):
        pty_attach.teardown(sid)


# --- the mind half --------------------------------------------------------

class RecordingTerminals(pty_attach.TmuxTerminals):
    """A tmux server that records the argv it would have run."""

    def __init__(self, tmp_path: Path):
        super().__init__("testmind", tmp_path)
        self.live: set[str] = set()
        self.calls: list[list[str]] = []

    def alive(self, session_id: str) -> bool:
        return session_id in self.live

    def start(self, session_id, argv, *, env_overrides, cols, rows) -> None:
        self.live.add(session_id)

    def respawn(self, session_id, argv, *, env_overrides) -> None:
        pass

    def _tmux_detached(self, *args: str) -> None:
        self.calls.append(list(args))

    def popup(self) -> list[str]:
        for args in self.calls:
            if args and args[0] == "display-popup":
                return args
        raise AssertionError(f"no display-popup among {[a[:1] for a in self.calls]}")

    def popup_body(self) -> str:
        """The text the recorded display-popup would have shown."""
        script = self.popup()[-1]
        for token in script.split():
            if token.strip("';").endswith(".popup"):
                return Path(token.strip("';")).read_text()
        raise AssertionError(f"no body file in {script!r}")


def _echo_spawn(**kwargs):
    cols, rows = pty_attach.clamp_winsize(kwargs.get("cols", 80), kwargs.get("rows", 24))
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = subprocess.Popen(["cat"], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd)
    os.close(slave_fd)
    return proc, master_fd


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A mind with one attached terminal and a rotation that succeeds."""
    monkeypatch.setenv("OWNER_NAME", "daniel")
    monkeypatch.setattr(pty_attach, "_NOTICE_DIR", tmp_path / "notices")
    terminals = RecordingTerminals(tmp_path)
    app = FastAPI()
    pty_attach.install_pty_attach(
        app, mind_name="testmind", terminals=terminals,
        spawn=_echo_spawn, rotate=lambda **kw: True,
    )
    client = TestClient(app)
    with client.websocket_connect("/sessions/s1/attach-pty?model=sonnet&resume_sid=c1"):
        pass
    terminals.live.add("s1")
    return client, terminals


def _rotate(client, last_exchange=None):
    payload = {"new_claude_sid": "conv-2", "model": "sonnet"}
    if last_exchange is not None:
        payload["last_exchange"] = last_exchange
    return client.post("/sessions/s1/rotate-pty", json=payload)


# --- the gateway half -----------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(
        ModelRegistry({"anthropic": Provider(name="anthropic")}, {"opus": "anthropic"})
    )
    await mgr.start()
    return mgr


async def _seed_terminal_session(mgr: SessionManager, client_ref: str) -> str:
    session_id = "sess-" + client_ref
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 created_at, last_active, status, mind_id)
           VALUES (?, 'web', 'terminal', 'opus', 'conv-old', ?, ?, 'running', 'ada')""",
        (session_id, now, now),
    )
    await mgr._db.execute(
        """INSERT INTO active_sessions (client_type, client_ref, session_id)
           VALUES ('web', ?, ?)""",
        (client_ref, session_id),
    )
    await mgr._db.commit()
    return session_id


@contextlib.contextmanager
def _real_rotation_paths():
    """Drive the real rotation, stubbing only what a temp DB cannot supply.

    The broker's mind row and the prompt composer need a registered mind and
    a populated KG; everything the notice depends on stays real.
    """
    async def mind_row(_db, mind_id):
        return {"name": mind_id, "model": "opus"}

    async def blocks(**_kw):
        return "<soul>seed</soul>"

    with patch("comms.broker.get_mind_by_id", mind_row), \
            patch("comms.bootstrap_loader.compose_prompt_blocks", blocks):
        yield


async def _seed_turns(mgr: SessionManager, session_id: str, turns: list[tuple]) -> None:
    base = time.time()
    for n, (role, content) in enumerate(turns):
        await mgr._db.execute(
            "INSERT INTO session_turns (session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, role, content, base + n),
        )
    await mgr._db.commit()


# 1 ------------------------------------------------------------------------
def test_a_rotation_that_has_begun_tells_the_attached_terminal(wired):
    client, terminals = wired

    # The gateway half: starting a terminal rotation sends the notice before
    # the minutes of composition that precede the pane respawn.
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                await _seed_terminal_session(mgr, "terminal-abc")
                sent: list[dict] = []

                async def capture(**kwargs):
                    sent.append(kwargs)
                    return True

                async def rotate(**kwargs):
                    return True

                mgr._pty_notice_on_mind = capture  # type: ignore[assignment]
                mgr._rotate_pty_on_mind = rotate  # type: ignore[assignment]
                with _real_rotation_paths():
                    await mgr.arm_rotation("web", "terminal-abc")

                assert sent, "the rotation never told the terminal it had begun"
                assert sent[0]["text"] == ROTATING_NOTICE
            finally:
                await mgr.shutdown()

    _run(scenario())

    # The mind half: that text reaches the pane as a popup that closes itself
    # rather than sitting on the user's keyboard.
    resp = client.post("/sessions/s1/pty-notice", json={"text": ROTATING_NOTICE})
    assert resp.json() == {"session_id": "s1", "shown": True}
    assert ROTATING_NOTICE in terminals.popup_body()
    assert "sleep" in terminals.popup()[-1]
    assert terminals.popup()[terminals.popup().index("-t") + 1] == "testmind-s1"


# 2 ------------------------------------------------------------------------
def test_the_rotated_terminal_leads_with_the_rotation_line(wired, tmp_path):
    client, terminals = wired

    _rotate(client, {"user": "what broke?", "assistant": "the pane did"})

    body = terminals.popup_body()
    assert pty_notice.ROTATED_TEXT in body
    assert body.index(pty_notice.ROTATED_TEXT) < body.index("what broke?")
    # Held for reading, and aimed at the session that actually rotated.
    popup = terminals.popup()
    assert "less" in popup[-1]
    assert popup[popup.index("-t") + 1] == "testmind-s1"

    # A rotation that did not happen has nothing to announce: claiming one
    # would tell the user their context reset when it did not.
    app = FastAPI()
    declining = RecordingTerminals(tmp_path)
    declining.live.add("s2")
    pty_attach.install_pty_attach(
        app, mind_name="testmind", terminals=declining,
        spawn=_echo_spawn, rotate=lambda **kw: False,
    )
    with TestClient(app) as failed:
        with failed.websocket_connect("/sessions/s2/attach-pty?model=sonnet&resume_sid=c1"):
            pass
        failed.post("/sessions/s2/rotate-pty", json={"new_claude_sid": "conv-2"})
    assert not any(a and a[0] == "display-popup" for a in declining.calls)


# 3 ------------------------------------------------------------------------
def test_the_last_exchange_is_replayed_under_that_line(wired):
    client, terminals = wired

    # The gateway half: the exchange comes off the durable turn ledger, and
    # it is the *last* one — a pair, not the newest of each role scavenged
    # from opposite ends of a session that outlives every rotation.
    async def scenario() -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_terminal_session(mgr, "terminal-abc")
                await _seed_turns(mgr, sid, [
                    ("user", "an old question"),
                    ("assistant", "an old answer from a previous conversation"),
                    ("user", "why is the terminal blank"),
                    ("assistant", "because respawn-pane drops the history"),
                ])
                return await mgr._last_exchange(sid)
            finally:
                await mgr.shutdown()

    exchange = _run(scenario())
    assert exchange == {
        "user": "why is the terminal blank",
        "assistant": "because respawn-pane drops the history",
    }

    # The mind half: both sides of it land in the popup.
    _rotate(client, exchange)
    body = terminals.popup_body()
    assert "why is the terminal blank" in body
    assert "because respawn-pane drops the history" in body
    assert "an old answer from a previous conversation" not in body


# 4 ------------------------------------------------------------------------
def test_a_very_long_reply_is_trimmed_to_its_last_fifty_lines(wired):
    client, terminals = wired
    reply = "\n".join(f"line-{n}" for n in range(200))

    _rotate(client, {"user": "dump it", "assistant": reply})

    body = terminals.popup_body()
    assert "line-199" in body and "line-150" in body
    assert "line-149" not in body and "line-1\n" not in body


# 5 ------------------------------------------------------------------------
def test_with_no_prior_exchange_the_rotation_line_stands_alone(wired):
    client, terminals = wired

    _rotate(client, None)

    body = terminals.popup_body()
    assert pty_notice.ROTATED_TEXT in body
    assert body.strip().endswith(f"── {pty_notice.ROTATED_TEXT} ──\x1b[0m")


# 6 ------------------------------------------------------------------------
@pytest.mark.parametrize("harness", ["claude_cli", "codex_cli"])
def test_codex_draws_the_same_notices_as_claude(harness):
    # The recap is composed above the adapters so the two cannot drift on the
    # wording — which only holds while both adapters actually install the
    # shared surface. A harness that stops doing so loses every notice
    # silently, so that wiring is what this pins.
    import importlib

    # The adapters resolve their mind folder from MIND_NAME at import time.
    os.environ.setdefault("MIND_NAME", "example")
    module = importlib.import_module(f"minds.harness.{harness}")
    routes = {r.path for r in module.app.routes}
    assert "/sessions/{session_id}/pty-notice" in routes
    assert "/sessions/{session_id}/rotate-pty" in routes
    # The notice is drawn by the shared tmux layer, not by the harness.
    assert isinstance(module.TERMINALS, pty_attach.TmuxTerminals)
    assert type(module.TERMINALS).notice is pty_attach.TmuxTerminals.notice
