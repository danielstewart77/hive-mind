"""Browser-terminal attach for containerized minds, tmux-backed.

Two halves. ``minds.pty_attach`` owns the plumbing — the session-owned
handle, eviction, teardown, the rotate route — and each mind supplies the
argv its own CLI needs. The cases here are the ones that were actually
broken in the field: a mind with no route at all (Hex answered the handshake
with a bare 403), a session's first turn spawned with ``--resume`` on a
conversation id that has no transcript, and a Codex mind handed a
conversation id its CLI cannot adopt.

The tmux server itself is stubbed here so these run anywhere; the real one
is exercised in ``test_tmux_terminal.py``.
"""

import fcntl
import json
import os
import pty
import struct
import subprocess
import termios
from pathlib import Path
from types import SimpleNamespace as types_SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from minds import pty_attach


@pytest.fixture(autouse=True)
def _clean_ptys():
    pty_attach.PTYS.clear()
    yield
    for sid in list(pty_attach.PTYS):
        pty_attach.teardown(sid)


class FakeTerminals(pty_attach.TmuxTerminals):
    """A tmux server that records instead of running one."""

    def __init__(self):
        super().__init__("testmind", Path("/tmp"))
        self.started: list[tuple[str, list[str]]] = []
        self.respawned: list[tuple[str, list[str]]] = []
        self.killed: list[str] = []
        self.live: set[str] = set()

    def alive(self, session_id: str) -> bool:
        return session_id in self.live

    def kill(self, session_id: str) -> bool:
        self.killed.append(session_id)
        return bool(self.live.discard(session_id) or True)

    def start(self, session_id, argv, *, env_overrides, cols, rows) -> None:
        self.started.append((session_id, argv))
        self.live.add(session_id)

    def respawn(self, session_id, argv, *, env_overrides) -> None:
        self.respawned.append((session_id, argv))


def _echo_spawn(**kwargs):
    """A pty running `cat` — echoes whatever the socket writes.

    Stands in for a tmux client: the route only ever sees a process and a
    master fd, whichever side of tmux they came from.
    """
    cols, rows = pty_attach.clamp_winsize(kwargs.get("cols", 80), kwargs.get("rows", 24))
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = subprocess.Popen(["cat"], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd)
    os.close(slave_fd)
    return proc, master_fd


def _app(spawn, rotate=None, terminals=None):
    app = FastAPI()
    pty_attach.install_pty_attach(
        app, mind_name="testmind", terminals=terminals or FakeTerminals(),
        spawn=spawn, rotate=rotate,
    )
    return app


def _get_winsize(fd: int) -> tuple[int, int]:
    rows, cols, _, _ = struct.unpack(
        "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    )
    return cols, rows


def _read_until(ws, needle: bytes, frames: int = 10) -> bytes:
    """Read frames until ``needle`` shows up — pty output arrives in pieces."""
    seen = b""
    for _ in range(frames):
        seen += ws.receive_bytes()
        if needle in seen:
            return seen
    raise AssertionError(f"{needle!r} never arrived; got {seen!r}")


def _handle(master_fd: int, cols: int = 80, rows: int = 24) -> pty_attach._PtyHandle:
    """A handle around a bare pty — no subprocess needed to test geometry."""
    handle = pty_attach._PtyHandle("sid", FakeTerminals(), "conv", cols, rows)
    handle.master_fd = master_fd
    return handle


class TestConversationFlags:
    """A conversation id the gateway minted has no transcript until its
    first process writes one. ``--resume`` on that id is how a session's
    first turn died with "No conversation found"."""

    def test_declares_the_id_when_no_transcript_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert pty_attach.claude_conversation_flags("conv-1", Path("/usr/src/app")) == [
            "--session-id", "conv-1",
        ]

    def test_resumes_when_a_transcript_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        slug = "-usr-src-app"
        (tmp_path / "projects" / slug).mkdir(parents=True)
        (tmp_path / "projects" / slug / "conv-2.jsonl").write_text("{}\n")
        assert pty_attach.claude_conversation_flags("conv-2", Path("/usr/src/app")) == [
            "--resume", "conv-2",
        ]

    def test_slug_folds_underscores_and_dots(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        slug = "-home-hivemind-hive-mind-x"
        (tmp_path / "projects" / slug).mkdir(parents=True)
        (tmp_path / "projects" / slug / "conv-3.jsonl").write_text("{}\n")
        assert pty_attach.claude_conversation_flags(
            "conv-3", Path("/home/hivemind/hive_mind.x")
        ) == ["--resume", "conv-3"]


class TestAttachRoute:
    def test_attach_without_a_conversation_id_is_refused(self):
        client = TestClient(_app(_echo_spawn))
        with pytest.raises(Exception) as excinfo:
            with client.websocket_connect("/sessions/s1/attach-pty?model=opus") as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 1008
        assert not pty_attach.PTYS

    def test_bytes_round_trip_through_the_pty(self):
        client = TestClient(_app(_echo_spawn))
        with client.websocket_connect("/sessions/s2/attach-pty?model=opus&resume_sid=c2") as ws:
            ws.send_bytes(b"hello\n")
            assert b"hello" in ws.receive_bytes()

    def test_the_conversation_outlives_the_socket_and_a_reattach_adopts_it(self):
        """The terminal belongs to the session, not the socket. A closed tab
        ends the *client*; the next attach starts a fresh one on the same
        handle, and tmux paints the current screen into it — which is why
        nothing here has to remember bytes."""
        terminals = FakeTerminals()
        client = TestClient(_app(_echo_spawn, terminals=terminals))
        with client.websocket_connect("/sessions/s3/attach-pty?model=opus&resume_sid=c3") as ws:
            ws.send_bytes(b"first\n")
            assert b"first" in ws.receive_bytes()

        handle = pty_attach.PTYS["s3"]
        assert handle.detached_at is not None
        assert handle.proc is None          # the client ended with the socket

        with client.websocket_connect("/sessions/s3/attach-pty?model=opus&resume_sid=c3") as ws:
            ws.send_bytes(b"second\n")
            assert b"second" in _read_until(ws, b"second")
            assert pty_attach.PTYS["s3"] is handle   # same conversation
            assert handle.proc is not None           # new client

    def test_a_second_attach_evicts_the_first(self):
        """One conversation, one keyboard — and the displaced tile is told it
        was replaced rather than that its terminal exited."""
        client = TestClient(_app(_echo_spawn))
        with pytest.raises(Exception) as excinfo:
            with client.websocket_connect("/sessions/s8/attach-pty?model=opus&resume_sid=c8") as first:
                first.send_bytes(b"one\n")
                first.receive_bytes()
                with client.websocket_connect("/sessions/s8/attach-pty?model=opus&resume_sid=c8"):
                    # Whatever the pty had already echoed drains first; the
                    # eviction is what ends the stream.
                    for _ in range(10):
                        first.receive_bytes()
        assert excinfo.value.code == 1012

    def test_a_refused_spawn_closes_1008_and_leaves_no_handle(self):
        def _refuse(**kwargs):
            raise pty_attach.PtyUnavailable("this conversation has not started yet")

        client = TestClient(_app(_refuse))
        with pytest.raises(Exception) as excinfo:
            with client.websocket_connect("/sessions/s4/attach-pty?model=opus&resume_sid=c4") as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 1008

    def test_a_failed_spawn_closes_1011(self):
        def _boom(**kwargs):
            raise RuntimeError("no such binary")

        client = TestClient(_app(_boom))
        with pytest.raises(Exception) as excinfo:
            with client.websocket_connect("/sessions/s5/attach-pty?model=opus&resume_sid=c5") as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 1011

    def test_teardown_kills_the_tmux_session_not_just_the_client(self):
        """Detaching the client alone would leave the conversation running
        with nobody able to reach it."""
        terminals = FakeTerminals()
        client = TestClient(_app(_echo_spawn, terminals=terminals))
        with client.websocket_connect("/sessions/s6/attach-pty?model=opus&resume_sid=c6") as ws:
            ws.send_bytes(b"x\n")
            ws.receive_bytes()

        assert pty_attach.teardown("s6") is True
        assert terminals.killed == ["s6"]
        assert "s6" not in pty_attach.PTYS

    def test_the_session_env_reaches_the_spawn(self):
        """Without a client ref in the pane the Stop hook's rotation check
        bails on every fire and a terminal conversation never rotates."""
        captured = {}

        def _spawn(**kwargs):
            captured.update(kwargs)
            return _echo_spawn(**kwargs)

        client = TestClient(_app(_spawn))
        with client.websocket_connect(
            "/sessions/s9/attach-pty?model=opus&resume_sid=c9&owner_type=terminal"
            "&owner_ref=daniel&harness_sid=thread-3&cols=100&rows=30"
        ) as ws:
            ws.send_bytes(b"y\n")
            ws.receive_bytes()

        assert captured["client_ref"] == "s9"   # falls back to the session id
        assert captured["owner_type"] == "terminal"
        assert captured["owner_ref"] == "daniel"
        assert captured["harness_sid"] == "thread-3"
        assert (captured["cols"], captured["rows"]) == (100, 30)

    def test_a_heartbeat_keeps_a_half_open_socket_detectable(self, monkeypatch):
        # A mobile network blip leaves the socket half-open; the browser goes
        # on reporting it OPEN. Missing beats is how the tile knows to
        # reattach instead of sitting black.
        monkeypatch.setattr(pty_attach, "_PTY_KEEPALIVE_S", 0.05)
        client = TestClient(_app(_echo_spawn))
        with client.websocket_connect("/sessions/s10/attach-pty?model=opus&resume_sid=c10") as ws:
            assert ws.receive_bytes() == b"\x00"

    def test_winsize_is_clamped_to_something_a_tui_can_render(self):
        assert pty_attach.clamp_winsize(0, 0) == (20, 5)
        assert pty_attach.clamp_winsize(9999, 9999) == (500, 200)
        assert pty_attach.clamp_winsize(100, 30) == (100, 30)


class TestResizeFrames:
    """A resize retargets the client's pty; tmux answers the SIGWINCH by
    repainting its own screen model, so the tile ends up holding a screen
    drawn for its new geometry even when the app inside never redraws."""

    def test_control_frame_resize_retargets_the_pty(self):
        master_fd, slave_fd = pty.openpty()
        try:
            handle = _handle(master_fd, cols=80, rows=24)

            assert pty_attach._control_frame(
                handle, '{"type":"resize","cols":44,"rows":27}') is True

            assert (handle.cols, handle.rows) == (44, 27)
            assert _get_winsize(slave_fd) == (44, 27)
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_control_frame_swallows_a_malformed_resize(self):
        master_fd, slave_fd = pty.openpty()
        try:
            handle = _handle(master_fd, cols=90, rows=25)
            pty_attach.set_winsize(master_fd, 90, 25)

            assert pty_attach._control_frame(
                handle, '{"type":"resize","cols":"wide"}') is True

            assert _get_winsize(slave_fd) == (90, 25)  # unchanged
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_control_frame_rejects_plain_text_and_other_json(self):
        master_fd, slave_fd = pty.openpty()
        try:
            handle = _handle(master_fd)
            assert pty_attach._control_frame(handle, "ls -la\n") is False
            assert pty_attach._control_frame(handle, "{not json") is False
            assert pty_attach._control_frame(handle, '{"type":"ping"}') is False
            assert pty_attach._control_frame(handle, '"just a string"') is False
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_a_degenerate_resize_clamps_rather_than_failing(self):
        master_fd, slave_fd = pty.openpty()
        try:
            handle = _handle(master_fd, cols=80, rows=24)
            handle.resize(1, 1)
            assert (handle.cols, handle.rows) == (20, 5)
            assert _get_winsize(slave_fd) == (20, 5)
        finally:
            os.close(master_fd)
            os.close(slave_fd)


class TestRotateRoute:
    """A rotation replaces the conversation, not the session and not the
    terminal, so nothing above tmux is told anything: the browser tile keeps
    typing into the same pane under the same id."""

    def _rotating_app(self, calls, result=True):
        def _rotate(**kwargs):
            calls.append(kwargs)
            return result
        return _app(_echo_spawn, rotate=_rotate)

    def test_rotation_repoints_the_live_handle(self):
        calls: list[dict] = []
        client = TestClient(self._rotating_app(calls))
        with client.websocket_connect("/sessions/r1/attach-pty?model=opus&resume_sid=old") as ws:
            ws.send_bytes(b"x\n")
            ws.receive_bytes()
            resp = client.post("/sessions/r1/rotate-pty", json={
                "new_claude_sid": "new-conv", "model": "sonnet",
                "system_prompt": "carry-forward", "client_ref": "r1",
            })

            assert resp.json() == {
                "session_id": "r1", "rotated": True, "claude_sid": "new-conv",
            }
            assert pty_attach.PTYS["r1"].conversation_id == "new-conv"
            assert calls[0]["system_prompt"] == "carry-forward"
            # The tile is undisturbed: same socket, same pty, still typing.
            ws.send_bytes(b"after\n")
            assert b"after" in _read_until(ws, b"after")

    def test_rotation_declines_when_no_tile_is_open(self):
        calls: list[dict] = []
        client = TestClient(self._rotating_app(calls))
        resp = client.post("/sessions/r2/rotate-pty", json={"new_claude_sid": "new"})
        assert resp.json()["rotated"] is False
        assert calls == []

    def test_rotation_needs_a_conversation_id_to_rotate_onto(self):
        client = TestClient(self._rotating_app([]))
        resp = client.post("/sessions/r3/rotate-pty", json={})
        assert resp.status_code == 400

    def test_a_mind_with_no_live_terminal_reports_rotated_false(self):
        client = TestClient(self._rotating_app([], result=False))
        with client.websocket_connect("/sessions/r4/attach-pty?model=opus&resume_sid=old") as ws:
            ws.send_bytes(b"x\n")
            ws.receive_bytes()
            resp = client.post("/sessions/r4/rotate-pty", json={"new_claude_sid": "new"})
            assert resp.json()["rotated"] is False
            assert pty_attach.PTYS["r4"].conversation_id == "old"


class TestSeededPaneCommand:
    """A composed carry-forward is tens of thousands of characters. Put in
    the tmux command it comes back as "command too long"; put in one argv
    entry it hits MAX_ARG_STRLEN and the pane dies a second after tmux starts
    it, which looks exactly like a rotation that worked."""

    def test_the_seed_reaches_the_pane_without_riding_in_the_command(self, tmp_path):
        seed = "carry-forward " * 4000
        seed_file = tmp_path / "rotation-seeds" / "sess-9.txt"

        cmd = pty_attach.seeded_pane_command(
            ["claude", "--model", "opus"], seed, seed_file,
            seed_flag="--append-system-prompt",
        )

        assert len(" ".join(cmd)) < 1000, "the seed is still in the command tmux parses"
        assert seed_file.read_text() == seed
        assert str(seed_file) in cmd[-1]
        assert "--append-system-prompt" in cmd[-1]

    def test_a_harness_without_a_prompt_flag_takes_it_positionally(self, tmp_path):
        # codex has no --append-system-prompt; the carry-forward is its
        # opening turn instead.
        seed_file = tmp_path / "seed.txt"
        cmd = pty_attach.seeded_pane_command(["codex"], "carry-forward", seed_file)
        assert cmd[-1].endswith('exec codex "$seed"')

    def test_an_oversized_seed_is_trimmed_to_what_exec_can_carry(self, tmp_path):
        seed = ("head " * 40000) + "THE-PENDING-TURNS"
        seed_file = tmp_path / "sess-big.txt"

        pty_attach.seeded_pane_command(["claude"], seed, seed_file)

        written = seed_file.read_text()
        assert len(written) <= pty_attach.MAX_SEED_CHARS
        assert written.endswith("THE-PENDING-TURNS")  # the tail is what continues

    def test_a_rotation_with_no_seed_runs_the_harness_directly(self, tmp_path):
        argv = ["claude", "--model", "opus"]
        assert pty_attach.seeded_pane_command(
            argv, "", tmp_path / "unused.txt") == argv


class TestMirroredTurns:
    """The interactive terminal only shows what its own harness process drew,
    so a Telegram turn would leave an open tile silently out of sync with
    what was actually said."""

    def test_a_chat_turn_is_overlaid_on_the_attached_tile(self):
        client = TestClient(_app(_echo_spawn))
        with client.websocket_connect("/sessions/m1/attach-pty?model=opus&resume_sid=c-m1") as ws:
            ws.send_bytes(b"warmup\n")
            ws.receive_bytes()

            assert pty_attach.mirror_turn(
                "m1", mind_name="nagatha", assistant_texts=["on it"],
                user_text="status?", surface="telegram",
            ) is True
            painted = _read_until(ws, b"on it").decode()

        assert "status?" in painted
        assert "[telegram] nagatha:" in painted
        assert "on it" in painted

    def test_nothing_is_written_when_no_tile_is_watching(self):
        assert pty_attach.mirror_turn(
            "nobody", mind_name="nagatha", assistant_texts=["on it"]) is False

    def test_a_turn_with_no_assistant_text_paints_nothing(self):
        client = TestClient(_app(_echo_spawn))
        with client.websocket_connect("/sessions/m2/attach-pty?model=opus&resume_sid=c-m2") as ws:
            ws.send_bytes(b"warmup\n")
            ws.receive_bytes()
            assert pty_attach.mirror_turn(
                "m2", mind_name="nagatha", assistant_texts=[]) is False


class TestClaudeCliWiring:
    """The Claude harness's stream-json spawn and its terminal must agree on
    how a conversation id becomes CLI flags, or the two processes end up on
    different conversations."""

    @pytest.fixture
    def claude(self):
        import minds.harness.claude_cli as impl
        return impl

    def test_the_routes_are_mounted(self, claude):
        paths = {getattr(r, "path", "") for r in claude.app.routes}
        assert "/sessions/{session_id}/attach-pty" in paths
        assert "/sessions/{session_id}/rotate-pty" in paths

    def test_pty_command_pins_the_gateway_conversation_id(self, claude, monkeypatch):
        monkeypatch.setattr(claude, "claude_conversation_flags",
                            lambda cid, _dir: ["--session-id", cid])
        cmd = claude._terminal_argv("opus", "conv-9")

        assert cmd[0] == "claude"
        assert "conv-9" in cmd
        # The TUI must not get print-mode flags — they disable the terminal.
        assert "-p" not in cmd
        assert "--input-format" not in cmd

    def test_rotation_starts_a_fresh_conversation_rather_than_resuming(self, claude):
        argv = claude._rotation_argv("sonnet", "conv-2")
        assert argv[argv.index("--session-id") + 1] == "conv-2"
        assert "--resume" not in argv

    def test_the_pane_carries_the_session_metadata_the_hooks_need(self, claude):
        env = claude._pane_env("chat-7", "telegram", "daniel")
        assert env["HIVEMIND_CLIENT_REF"] == "chat-7"
        assert env["HIVEMIND_OWNER_TYPE"] == "telegram"
        assert env["HIVEMIND_OWNER_REF"] == "daniel"
        assert env["HIVE_SURFACE"] == "terminal"
        # The agent view is a second session picker inside a surface that
        # already has one, re-hosting the conversation at the wrong geometry.
        assert env["CLAUDE_CODE_DISABLE_AGENT_VIEW"] == "1"

    def test_spawn_starts_the_terminal_then_attaches_a_client(self, claude, monkeypatch):
        started, attached = [], []
        monkeypatch.setattr(claude.TERMINALS, "start",
                            lambda sid, argv, **kw: started.append((sid, argv)))
        monkeypatch.setattr(
            claude.TERMINALS, "attach",
            lambda sid, **kw: (attached.append((sid, kw)),
                               (types_SimpleNamespace(pid=1, poll=lambda: None), -1))[1],
        )
        claude._spawn_pty(session_id="a1", model="opus", conversation_id="conv-9",
                          cols=100, rows=30)

        assert started[0][0] == "a1"
        assert started[0][1][0] == "claude"
        assert attached[0][1]["cols"] == 100

    def test_rotation_declines_when_there_is_no_live_terminal(self, claude, monkeypatch):
        monkeypatch.setattr(claude.TERMINALS, "alive", lambda sid: False)
        assert claude._rotate_pty(session_id="a2", new_claude_sid="conv-3") is False

    def test_rotation_respawns_the_pane_with_the_carry_forward(self, claude, monkeypatch, tmp_path):
        monkeypatch.setattr(claude, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(claude.TERMINALS, "alive", lambda sid: True)
        respawned = {}
        monkeypatch.setattr(
            claude.TERMINALS, "respawn",
            lambda sid, argv, **kw: respawned.update(sid=sid, argv=argv, env=kw),
        )

        assert claude._rotate_pty(
            session_id="a3", new_claude_sid="conv-4", model="sonnet",
            system_prompt="the summary", client_ref="a3",
        ) is True

        assert respawned["sid"] == "a3"
        assert "the summary" in (tmp_path / "rotation-seeds" / "conv-4.txt").read_text()
        assert respawned["env"]["env_overrides"]["HIVEMIND_CLIENT_REF"] == "a3"

    def test_a_chat_turn_yields_the_text_a_tile_would_have_missed(self, claude):
        event = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "spoken"},
            {"type": "tool_use", "name": "Bash"},
        ]}}
        assert claude._assistant_texts(event) == ["spoken"]
        assert claude._assistant_texts({"type": "result"}) == []


class TestCodexCliThreads:
    """Codex will not adopt a conversation id it was handed, so the gateway's
    id and the codex thread are two different things and the mapping lives in
    the mind. Planting the gateway's uuid as a thread id made every first
    turn a `codex exec resume <uuid>` against a thread that never existed."""

    @pytest.fixture
    def codex(self):
        import minds.harness.codex_cli as impl
        return impl

    def test_the_routes_are_mounted(self, codex):
        paths = {getattr(r, "path", "") for r in codex.app.routes}
        assert "/sessions/{session_id}/attach-pty" in paths
        assert "/sessions/{session_id}/rotate-pty" in paths

    def test_gateway_conversation_id_is_not_used_as_a_codex_thread(self, codex):
        codex.SESSIONS.clear()
        codex.THREADS.clear()
        client = TestClient(codex.app)
        client.post("/sessions", json={
            "session_id": "n1", "resume_sid": "gateway-uuid", "model": "gpt-5",
        })
        assert codex.SESSIONS["n1"]["thread_id"] is None

    def test_a_known_thread_is_rejoined_on_respawn(self, codex):
        codex.SESSIONS.clear()
        codex.THREADS.clear()
        codex.THREADS["n2"] = "codex-thread-7"
        client = TestClient(codex.app)
        client.post("/sessions", json={
            "session_id": "n2", "resume_sid": "gateway-uuid", "model": "gpt-5",
        })
        assert codex.SESSIONS["n2"]["thread_id"] == "codex-thread-7"

    @pytest.mark.asyncio
    async def test_release_stream_preserves_thread_for_resume(self, codex, monkeypatch):
        codex.SESSIONS.clear()
        codex.THREADS.clear()
        codex.SESSIONS["n-release"] = {"proc": object()}
        codex.THREADS["n-release"] = "codex-thread-release"

        reaped = []

        async def _reap(proc):
            reaped.append(proc)

        monkeypatch.setattr(codex, "_reap_proc", _reap)
        result = await codex.release_session("n-release", "stream")

        assert result["released"] is True
        assert "n-release" not in codex.SESSIONS
        assert codex.THREADS["n-release"] == "codex-thread-release"
        assert len(reaped) == 1

    def test_attach_before_the_first_turn_launches_bare_codex(self, codex, monkeypatch):
        """No thread yet: launch bare `codex` (no `resume`) instead of
        refusing. `app-server`'s thread/start mints an id without ever
        writing the rollout `codex resume` needs, so the id has to come from
        the rollout codex itself writes on the first real turn."""
        codex.THREADS.clear()
        started, watched = {}, {}
        monkeypatch.setattr(codex.TERMINALS, "alive", lambda sid: False)
        monkeypatch.setattr(codex.TERMINALS, "start",
                            lambda sid, argv, **kw: started.update(argv=argv))
        monkeypatch.setattr(
            codex.TERMINALS, "attach",
            lambda sid, **kw: (types_SimpleNamespace(pid=1, poll=lambda: None), -1),
        )
        monkeypatch.setattr(codex, "_watch_for_new_thread_in_background",
                            lambda sid, before: watched.update(session_id=sid))

        codex._spawn_pty(session_id="n3", model="gpt-5",
                         conversation_id="gateway-uuid", cols=80, rows=24)

        assert "resume" not in started["argv"]
        assert watched["session_id"] == "n3"

    def test_the_gateways_copy_of_the_thread_id_wins_after_a_redeploy(
        self, codex, monkeypatch
    ):
        """THREADS dies with the container; the session row does not, so a
        tile reattaching after a redeploy is handed harness_sid."""
        codex.THREADS.clear()
        monkeypatch.setattr(codex, "_rollout_exists", lambda tid: True)
        assert codex._resumable_thread("n6", "thread-from-gateway") == "thread-from-gateway"
        assert codex.THREADS["n6"] == "thread-from-gateway"

    def test_a_thread_whose_rollout_is_gone_is_discarded(self, codex, monkeypatch, tmp_path):
        """A thread id can outlive the container that minted it — a migration
        or a redeploy onto a fresh volume. `codex resume` on a missing
        rollout dies within a second of tmux starting it, which reads
        identically to a hung terminal."""
        monkeypatch.setattr(codex, "CODEX_HOME", tmp_path)  # no rollouts at all
        codex.THREADS.clear()
        codex.THREADS["n5"] = "stale-thread-id"

        assert codex._resumable_thread("n5", None) is None
        assert "n5" not in codex.THREADS

    def test_rollout_exists_checks_this_codex_homes_sessions_dir(
        self, codex, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(codex, "CODEX_HOME", tmp_path)
        sessions_dir = tmp_path / "sessions" / "2026" / "07" / "26"
        sessions_dir.mkdir(parents=True)
        rollout = sessions_dir / "rollout-2026-07-26T00-00-00-deadbeef-dead-beef-dead-beefdeadbeef.jsonl"
        rollout.write_text("{}")

        assert codex._rollout_exists("deadbeef-dead-beef-dead-beefdeadbeef") is True
        assert codex._rollout_exists("00000000-0000-0000-0000-000000000000") is False

    def test_the_terminal_carries_the_same_provider_override_as_a_turn(
        self, codex, monkeypatch
    ):
        """An Ollama-backed mind whose TUI talked to the default provider
        would answer as a different model than its own turns do."""
        monkeypatch.setattr(codex, "PROVIDER", "ollama")
        monkeypatch.setitem(codex.RUNTIME_ENV, "OLLAMA_BASE_URL", "http://ollama:11434/v1")

        cmd = codex._terminal_argv("gpt-oss", "codex-thread-4")

        assert cmd[0] == "codex"
        assert cmd[-2:] == ["resume", "codex-thread-4"]
        for arg in codex._provider_args():
            assert arg in cmd

    def test_the_pane_carries_the_session_metadata_the_hooks_need(self, codex):
        env = codex._pane_env("chat-7", "telegram", "daniel")
        assert env["CLIENT_REF"] == "chat-7"
        assert env["OWNER_TYPE"] == "telegram"
        assert env["OWNER_REF"] == "daniel"
        assert env["HIVE_SURFACE"] == "terminal"
        assert env["CODEX_HOME"] == str(codex.CODEX_HOME)

    def test_rotation_starts_a_bare_thread_and_seeds_the_carry_forward(
        self, codex, monkeypatch, tmp_path
    ):
        """Codex cannot be handed a thread id, so the successor starts bare
        and the carry-forward rides in as its opening turn."""
        monkeypatch.setattr(codex, "CODEX_HOME", tmp_path)
        monkeypatch.setattr(codex.TERMINALS, "alive", lambda sid: True)
        monkeypatch.setattr(codex, "_watch_for_new_thread_in_background",
                            lambda sid, before: None)
        codex.THREADS["n7"] = "old-thread"
        respawned = {}
        monkeypatch.setattr(codex.TERMINALS, "respawn",
                            lambda sid, argv, **kw: respawned.update(argv=argv))

        assert codex._rotate_pty(session_id="n7", new_claude_sid="conv-9",
                                 model="gpt-5", system_prompt="the summary") is True

        # The old thread must go: a reattach that resumed it would undo the
        # rotation the gateway just paid for.
        assert "n7" not in codex.THREADS
        assert "resume" not in " ".join(respawned["argv"])
        assert "the summary" in (tmp_path / "rotation-seeds" / "n7.txt").read_text()

    def test_kill_forgets_the_thread(self, codex):
        codex.SESSIONS.clear()
        codex.THREADS["n4"] = "codex-thread-9"
        client = TestClient(codex.app)
        client.delete("/sessions/n4")
        assert "n4" not in codex.THREADS


class TestTuiFirstRunFlags:
    """The interactive TUI's first-run gates, which ``claude -p`` skips.

    A containerized mind authenticated by ``CLAUDE_CODE_OAUTH_TOKEN`` talks
    fine over Telegram while its config dir has never completed onboarding or
    trusted its workspace. Opening the same mind in the browser terminal used
    to land on the theme-then-login wizard, which reads as "it's asking me to
    log into my Anthropic account" — first-run state, not credentials.
    """

    def _read(self, cfg: Path) -> dict:
        return json.loads((cfg / ".claude.json").read_text())

    def test_seeds_onboarding_and_trust_for_the_workspace(self, tmp_path):
        pty_attach.ensure_tui_first_run_flags(tmp_path, "/usr/src/app")

        data = self._read(tmp_path)
        assert data["hasCompletedOnboarding"] is True
        entry = data["projects"]["/usr/src/app"]
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["hasCompletedProjectOnboarding"] is True

    def test_seeds_the_bypass_permissions_acceptance(self, tmp_path):
        # Third gate: every mind spawns with bypassPermissions by design, and
        # the TUI stops on an acceptance prompt for it that -p never shows.
        pty_attach.ensure_tui_first_run_flags(tmp_path, "/usr/src/app")

        settings = json.loads((tmp_path / "settings.json").read_text())
        assert settings["skipDangerousModePermissionPrompt"] is True

    def test_preserves_existing_settings(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({
            "defaultMode": "bypassPermissions",
            "hooks": {"Stop": [{"command": "auto_remember.sh"}]},
        }))

        pty_attach.ensure_tui_first_run_flags(tmp_path, "/usr/src/app")

        settings = json.loads((tmp_path / "settings.json").read_text())
        assert settings["defaultMode"] == "bypassPermissions"
        assert settings["hooks"] == {"Stop": [{"command": "auto_remember.sh"}]}
        assert settings["skipDangerousModePermissionPrompt"] is True

    def test_preserves_everything_else_in_the_config(self, tmp_path):
        # The file is the harness's own live config — seeding a flag must not
        # cost the mind its oauth account, tips history or project entries.
        (tmp_path / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "mind@example.com"},
            "projects": {"/other": {"allowedTools": ["Bash"]}},
        }))

        pty_attach.ensure_tui_first_run_flags(tmp_path, "/usr/src/app")

        data = self._read(tmp_path)
        assert data["oauthAccount"] == {"emailAddress": "mind@example.com"}
        assert data["projects"]["/other"] == {"allowedTools": ["Bash"]}
        assert data["projects"]["/usr/src/app"]["hasTrustDialogAccepted"] is True

    def test_already_seeded_config_is_left_untouched(self, tmp_path):
        seeded = {
            "hasCompletedOnboarding": True,
            "projects": {"/usr/src/app": {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
            }},
        }
        path = tmp_path / ".claude.json"
        path.write_text(json.dumps(seeded))
        before = path.stat().st_mtime_ns

        pty_attach.ensure_tui_first_run_flags(tmp_path, "/usr/src/app")

        assert path.stat().st_mtime_ns == before  # no rewrite at all

    def test_a_corrupt_config_is_left_alone_rather_than_clobbered(self, tmp_path):
        path = tmp_path / ".claude.json"
        path.write_text("{not json")

        pty_attach.ensure_tui_first_run_flags(tmp_path, "/usr/src/app")

        assert path.read_text() == "{not json"
