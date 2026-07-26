"""Browser-terminal attach for containerized minds.

Two halves. ``minds.pty_attach`` owns the pty plumbing — the session-owned
handle, the scrollback replay, eviction, teardown — and each mind supplies a
spawn callable for its own harness. The cases here are the ones that were
actually broken in the field: a mind with no route at all (Hex answered the
handshake with a bare 403), a session's first turn spawned with ``--resume``
on a conversation id that has no transcript, and a Codex mind handed a
conversation id its CLI cannot adopt.
"""

import fcntl
import json
import os
import pty
import struct
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


def _echo_spawn(**kwargs):
    """A pty running `cat` — echoes whatever the socket writes."""
    return pty_attach.open_pty_process(
        ["cat"], env=dict(os.environ), cwd="/tmp",
        cols=kwargs.get("cols", 80), rows=kwargs.get("rows", 24),
    )


def _app(spawn):
    app = FastAPI()
    pty_attach.install_pty_attach(app, mind_name="testmind", spawn=spawn)
    return app


def _get_winsize(fd: int) -> tuple[int, int]:
    rows, cols, _, _ = struct.unpack(
        "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    )
    return cols, rows


def _handle(master_fd: int, cols: int = 80, rows: int = 24) -> pty_attach._PtyHandle:
    """A handle around a bare pty — no subprocess needed to test geometry."""
    return pty_attach._PtyHandle("sid", proc=None, master_fd=master_fd,
                                 conversation_id="conv", cols=cols, rows=rows)


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
            with client.websocket_connect("/sessions/s1/attach-pty") as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 1008
        assert not pty_attach.PTYS

    def test_bytes_round_trip_through_the_pty(self):
        client = TestClient(_app(_echo_spawn))
        with client.websocket_connect("/sessions/s2/attach-pty?resume_sid=c2") as ws:
            ws.send_bytes(b"hello\n")
            assert b"hello" in ws.receive_bytes()

    def test_the_terminal_outlives_the_socket_and_replays_scrollback(self):
        """The pty belongs to the session, not the socket: a closed tab must
        not take the turn with it, and reattaching repaints what was missed."""
        client = TestClient(_app(_echo_spawn))
        with client.websocket_connect("/sessions/s3/attach-pty?resume_sid=c3") as ws:
            ws.send_bytes(b"first\n")
            assert b"first" in ws.receive_bytes()

        handle = pty_attach.PTYS["s3"]
        assert handle.proc.poll() is None       # still running, unattached
        assert handle.detached_at is not None

        with client.websocket_connect("/sessions/s3/attach-pty?resume_sid=c3") as ws:
            assert b"first" in ws.receive_bytes()   # scrollback replay
            assert pty_attach.PTYS["s3"] is handle  # adopted, not respawned

    def test_a_refused_spawn_closes_1008_and_leaves_no_handle(self):
        def _refuse(**kwargs):
            raise pty_attach.PtyUnavailable("this conversation has not started yet")

        client = TestClient(_app(_refuse))
        with pytest.raises(Exception) as excinfo:
            with client.websocket_connect("/sessions/s4/attach-pty?resume_sid=c4") as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 1008
        assert not pty_attach.PTYS

    def test_a_failed_spawn_closes_1011(self):
        def _boom(**kwargs):
            raise RuntimeError("no such binary")

        client = TestClient(_app(_boom))
        with pytest.raises(Exception) as excinfo:
            with client.websocket_connect("/sessions/s5/attach-pty?resume_sid=c5") as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 1011

    def test_teardown_kills_the_process(self):
        client = TestClient(_app(_echo_spawn))
        with client.websocket_connect("/sessions/s6/attach-pty?resume_sid=c6") as ws:
            ws.send_bytes(b"x\n")
            ws.receive_bytes()
        proc = pty_attach.PTYS["s6"].proc
        assert pty_attach.teardown("s6") is True
        assert proc.poll() is not None
        assert pty_attach.teardown("s6") is False   # idempotent

    def test_winsize_is_clamped_to_something_a_tui_can_render(self):
        assert pty_attach.clamp_winsize(0, 0) == (20, 5)
        assert pty_attach.clamp_winsize(9999, 9999) == (500, 200)
        assert pty_attach.clamp_winsize(100, 30) == (100, 30)


class TestScrollbackGeometry:
    """Captured output carries hard line breaks at the width it was emitted
    for. Replaying an 80-column capture into a 44-column tile shreds it into
    ragged half-lines rather than reflowing, which is what a phone showed
    after a session had last been driven from a desktop."""

    def test_resize_to_a_new_width_drops_unreplayable_scrollback(self):
        master_fd, slave_fd = pty.openpty()
        try:
            handle = _handle(master_fd, cols=80, rows=24)
            handle.feed(b"x" * 80 + b"\r\n")
            assert handle.scrollback

            handle.resize(44, 27)

            assert bytes(handle.scrollback) == b""
            assert (handle.cols, handle.rows) == (44, 27)
            assert _get_winsize(slave_fd) == (44, 27)
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_resize_of_height_alone_keeps_scrollback(self):
        # Same width wraps identically, so the capture is still replayable
        # and a keyboard opening should not cost the tile its history.
        master_fd, slave_fd = pty.openpty()
        try:
            handle = _handle(master_fd, cols=80, rows=24)
            handle.feed(b"still readable\r\n")

            handle.resize(80, 40)

            assert bytes(handle.scrollback) == b"still readable\r\n"
            assert _get_winsize(slave_fd) == (80, 40)
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_resize_compares_clamped_widths(self):
        # A degenerate request clamps to the width it already had, so it must
        # not be mistaken for a width change and throw history away.
        master_fd, slave_fd = pty.openpty()
        try:
            handle = _handle(master_fd, cols=20, rows=24)
            handle.feed(b"narrow\r\n")

            handle.resize(1, 24)

            assert bytes(handle.scrollback) == b"narrow\r\n"
            assert handle.cols == 20
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_control_frame_resize_drops_stale_scrollback(self):
        master_fd, slave_fd = pty.openpty()
        try:
            handle = _handle(master_fd, cols=80, rows=24)
            handle.feed(b"wrapped for eighty columns\r\n")

            assert pty_attach._control_frame(
                handle, '{"type":"resize","cols":44,"rows":27}') is True

            assert bytes(handle.scrollback) == b""
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

    def test_reattaching_at_a_new_width_replays_nothing(self):
        client = TestClient(_app(_echo_spawn))
        with client.websocket_connect(
                "/sessions/s7/attach-pty?resume_sid=c7&cols=80&rows=24") as ws:
            ws.send_bytes(b"eighty\n")
            assert b"eighty" in ws.receive_bytes()

        with client.websocket_connect(
                "/sessions/s7/attach-pty?resume_sid=c7&cols=44&rows=27") as ws:
            assert pty_attach.PTYS["s7"].cols == 44
            assert not pty_attach.PTYS["s7"].scrollback
            ws.send_bytes(b"forty-four\n")
            assert b"forty-four" in ws.receive_bytes()


class TestClaudeCliWiring:
    """The Claude harness's stream-json spawn and its terminal must agree on
    how a conversation id becomes CLI flags, or the two processes end up on
    different conversations. An Ollama-backed mind is the same harness with
    a different runtime env, so this covers both."""

    @pytest.fixture
    def claude(self):
        import minds.harness.claude_cli as impl
        return impl

    def test_the_attach_route_is_mounted(self, claude):
        paths = {getattr(r, "path", "") for r in claude.app.routes}
        assert "/sessions/{session_id}/attach-pty" in paths

    def test_pty_command_pins_the_gateway_conversation_id(self, claude, monkeypatch):
        captured = {}

        def _fake_open(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return (types_SimpleNamespace(pid=1, poll=lambda: None), -1)

        monkeypatch.setattr(claude, "open_pty_process", _fake_open)
        claude._spawn_pty(session_id="a1", model="opus", conversation_id="conv-9",
                          cols=100, rows=30)

        cmd = captured["cmd"]
        assert cmd[0] == "claude"
        assert "conv-9" in cmd
        # The TUI must not get print-mode flags — they disable the terminal.
        assert "-p" not in cmd
        assert "--input-format" not in cmd
        assert (captured["cols"], captured["rows"]) == (100, 30)


class TestCodexCliThreads:
    """Codex will not adopt a conversation id it was handed, so the gateway's
    id and the codex thread are two different things and the mapping lives in
    the mind. Planting the gateway's uuid as a thread id made every first
    turn a `codex exec resume <uuid>` against a thread that never existed."""

    @pytest.fixture
    def codex(self):
        import minds.harness.codex_cli as impl
        return impl

    def test_the_attach_route_is_mounted(self, codex):
        paths = {getattr(r, "path", "") for r in codex.app.routes}
        assert "/sessions/{session_id}/attach-pty" in paths

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
        refusing — starting one here no longer forks a second conversation,
        since the thread the gateway sees is whatever THREADS is later told
        by the background watcher, not something minted up front."""
        codex.THREADS.clear()
        captured = {}
        watched = {}

        def _fake_open(cmd, **kwargs):
            captured["cmd"] = cmd
            return (types_SimpleNamespace(pid=1, poll=lambda: 0), -1)

        def _fake_watch(session_id, proc, before):
            watched["session_id"] = session_id

        monkeypatch.setattr(codex, "open_pty_process", _fake_open)
        monkeypatch.setattr(codex, "_watch_for_new_thread_in_background", _fake_watch)

        codex._spawn_pty(
            session_id="n3", model="gpt-5", conversation_id="gateway-uuid",
            cols=80, rows=24,
        )

        assert "resume" not in captured["cmd"]
        assert watched["session_id"] == "n3"

    def test_spawn_pty_discards_a_stale_thread_and_launches_bare_codex(
        self, codex, monkeypatch, tmp_path
    ):
        """A thread id can outlive the container that minted it — a
        migration or a redeploy onto a fresh volume. `codex resume` on a
        missing rollout dies within a second of the pty starting it, which
        reads identically to a hung terminal; noticing the rollout is gone
        and falling back to a fresh launch is the fix."""
        monkeypatch.setattr(codex, "CODEX_HOME", tmp_path)  # no rollouts at all
        codex.THREADS.clear()
        codex.THREADS["n5"] = "stale-thread-id"
        captured = {}

        def _fake_open(cmd, **kwargs):
            captured["cmd"] = cmd
            return (types_SimpleNamespace(pid=1, poll=lambda: 0), -1)

        monkeypatch.setattr(codex, "open_pty_process", _fake_open)
        monkeypatch.setattr(codex, "_watch_for_new_thread_in_background", lambda *a, **k: None)

        codex._spawn_pty(
            session_id="n5", model="gpt-5", conversation_id="gateway-uuid",
            cols=80, rows=24,
        )

        assert "resume" not in captured["cmd"]
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
        captured = {}

        def _fake_open(cmd, **kwargs):
            captured["cmd"] = cmd
            return (types_SimpleNamespace(pid=1, poll=lambda: None), -1)

        monkeypatch.setattr(codex, "open_pty_process", _fake_open)
        monkeypatch.setattr(codex, "_rollout_exists", lambda thread_id: True)
        monkeypatch.setattr(codex, "PROVIDER", "ollama")
        monkeypatch.setitem(codex.RUNTIME_ENV, "OLLAMA_BASE_URL", "http://ollama:11434/v1")
        codex.THREADS["b4"] = "codex-thread-4"
        codex._spawn_pty(session_id="b4", model="gpt-oss",
                         conversation_id="gateway-uuid", cols=80, rows=24)

        cmd = captured["cmd"]
        assert cmd[0] == "codex"
        assert cmd[-2:] == ["resume", "codex-thread-4"]
        for arg in codex._provider_args():
            assert arg in cmd

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

    def test_open_pty_process_seeds_the_config_dir_it_is_given(self, tmp_path):
        # Every mind's _spawn_pty routes through here, so seeding at this
        # chokepoint is what makes the fix apply to all of them at once.
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        proc, master_fd = pty_attach.open_pty_process(
            ["true"],
            env={"CLAUDE_CONFIG_DIR": str(tmp_path)},
            cwd=str(workspace),
            cols=80,
            rows=24,
        )
        try:
            data = self._read(tmp_path)
            assert data["hasCompletedOnboarding"] is True
            assert data["projects"][str(workspace)]["hasTrustDialogAccepted"] is True
        finally:
            proc.wait()
            os.close(master_fd)
