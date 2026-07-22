"""Browser-terminal attach for containerized minds.

Two halves. ``minds.pty_attach`` owns the pty plumbing — the session-owned
handle, the scrollback replay, eviction, teardown — and each mind supplies a
spawn callable for its own harness. The cases here are the ones that were
actually broken in the field: a mind with no route at all (Hex answered the
handshake with a bare 403), a session's first turn spawned with ``--resume``
on a conversation id that has no transcript, and a Codex mind handed a
conversation id its CLI cannot adopt.
"""

import os
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
        slug = "-home-daniel-hive-mind-x"
        (tmp_path / "projects" / slug).mkdir(parents=True)
        (tmp_path / "projects" / slug / "conv-3.jsonl").write_text("{}\n")
        assert pty_attach.claude_conversation_flags(
            "conv-3", Path("/home/daniel/hive_mind.x")
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


class TestAdaWiring:
    """Ada is the Claude side of the same route. Her stream-json spawn and
    her terminal must agree on how a conversation id becomes CLI flags, or
    the two processes end up on different conversations."""

    @pytest.fixture
    def ada(self):
        import minds.ada.implementation as impl
        return impl

    def test_the_attach_route_is_mounted(self, ada):
        paths = {getattr(r, "path", "") for r in ada.app.routes}
        assert "/sessions/{session_id}/attach-pty" in paths

    def test_pty_command_pins_the_gateway_conversation_id(self, ada, monkeypatch):
        captured = {}

        def _fake_open(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return (types_SimpleNamespace(pid=1, poll=lambda: None), -1)

        monkeypatch.setattr(ada, "open_pty_process", _fake_open)
        ada._spawn_pty(session_id="a1", model="opus", conversation_id="conv-9",
                       cols=100, rows=30)

        cmd = captured["cmd"]
        assert cmd[0] == "claude"
        assert "conv-9" in cmd
        # The TUI must not get print-mode flags — they disable the terminal.
        assert "-p" not in cmd
        assert "--input-format" not in cmd
        assert (captured["cols"], captured["rows"]) == (100, 30)


class TestNagathaCodexThreads:
    """Codex will not adopt a conversation id it was handed, so the gateway's
    id and the codex thread are two different things and the mapping lives in
    the mind. Planting the gateway's uuid as a thread id made every first
    turn a `codex exec resume <uuid>` against a thread that never existed."""

    @pytest.fixture
    def nagatha(self):
        import minds.nagatha.implementation as impl
        return impl

    def test_gateway_conversation_id_is_not_used_as_a_codex_thread(self, nagatha):
        nagatha.SESSIONS.clear()
        nagatha.THREADS.clear()
        client = TestClient(nagatha.app)
        client.post("/sessions", json={
            "session_id": "n1", "resume_sid": "gateway-uuid", "model": "gpt-5",
        })
        assert nagatha.SESSIONS["n1"]["thread_id"] is None

    def test_a_known_thread_is_rejoined_on_respawn(self, nagatha):
        nagatha.SESSIONS.clear()
        nagatha.THREADS.clear()
        nagatha.THREADS["n2"] = "codex-thread-7"
        client = TestClient(nagatha.app)
        client.post("/sessions", json={
            "session_id": "n2", "resume_sid": "gateway-uuid", "model": "gpt-5",
        })
        assert nagatha.SESSIONS["n2"]["thread_id"] == "codex-thread-7"

    def test_attach_before_the_first_turn_is_refused_not_forked(self, nagatha):
        nagatha.THREADS.clear()
        with pytest.raises(pty_attach.PtyUnavailable):
            nagatha._spawn_pty(
                session_id="n3", model="gpt-5", conversation_id="gateway-uuid",
                cols=80, rows=24,
            )

    def test_kill_forgets_the_thread(self, nagatha):
        nagatha.SESSIONS.clear()
        nagatha.THREADS["n4"] = "codex-thread-9"
        client = TestClient(nagatha.app)
        client.delete("/sessions/n4")
        assert "n4" not in nagatha.THREADS
