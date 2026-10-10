"""One mind server running every harness, routing each session by its own.

Every test drives ``minds.mind_server`` over HTTP. The stubs sit where the
process stops being ours: ``asyncio.create_subprocess_exec`` for a chat turn,
each adapter's tmux server for a pane, aiohttp for the inference proxy, and a
temporary directory for every harness's home.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import shutil
import struct
import subprocess
import termios
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from minds import mind_server, models_api, pty_attach, runtime_api
from minds.harness import claude_cli, codex_cli, dsh_cli

SESSION = {"Authorization": "Bearer test-mind-session-token"}  # secret-guard: allow
ADMIN_TOKEN = "test-admin-token"  # secret-guard: allow
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "transcripts"
CODEX_ROLLOUT = "rollout-2026-06-01T16-11-51-019e83f4-b0b3-7a61-b649-392094c6ef10.jsonl"
CODEX_THREAD = "019e83f4-b0b3-7a61-b649-392094c6ef10"

# A pid no process can hold, so a reaper that signals a fake process group
# signals nothing.
NO_SUCH_PID = 2_000_000_000


# ---------------------------------------------------------------------------
# The proxy, as the far end of an aiohttp session
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _Proxy:
    """Answers `/v1/models?harness=X` from a table, recording every URL."""

    def __init__(self, listings: dict[str, list[dict]]):
        self.listings = listings
        self.seen: list[str] = []

    def __call__(self, *args, headers=None, **kwargs):
        proxy = self

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, url, **kw):
                proxy.seen.append(url)
                harness = url.rsplit("harness=", 1)[-1] if "harness=" in url else ""
                return _Resp({"data": proxy.listings.get(harness, [])})

        return _Session()


@pytest.fixture
def proxy(monkeypatch):
    def install(listings: dict[str, list[dict]]) -> _Proxy:
        fake = _Proxy(listings)
        monkeypatch.setattr(models_api.aiohttp, "ClientSession", fake)
        return fake

    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.test")
    monkeypatch.setenv("MIND_PROXY_KEY", "hmp-test")
    mind_server._WINDOWS.clear()
    return install


@pytest.fixture
def homes(monkeypatch, tmp_path):
    """Every harness's home in a temporary directory, and clean tables."""
    paths = {
        "claude": tmp_path / "claude", "codex": tmp_path / "codex", "dsh": tmp_path / "dsh",
        "work": tmp_path / "work",
    }
    for path in paths.values():
        path.mkdir()
    monkeypatch.setattr(claude_cli, "CONFIG_DIR", paths["claude"])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(paths["claude"]))
    monkeypatch.setattr(codex_cli, "CODEX_HOME", paths["codex"])
    monkeypatch.setattr(dsh_cli, "DSH_HOME", paths["dsh"])
    monkeypatch.setattr(dsh_cli, "SPAWN_DIR", paths["work"])
    for adapter in (claude_cli, codex_cli, dsh_cli):
        adapter.SESSIONS.clear()
    codex_cli.THREADS.clear()
    mind_server.HARNESS_OF.clear()
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    yield paths
    for sid in list(pty_attach.PTYS):
        pty_attach.PTYS.pop(sid, None)


# ---------------------------------------------------------------------------
# A spawned harness process
# ---------------------------------------------------------------------------

class _Stdout:
    """Lines a harness writes, released only once it has been sent a turn.

    The release waits a beat longer than the claude adapter's idle drain
    polls for, so the drain backs off and the turn's own reader gets them.
    """

    def __init__(self, lines: list[bytes], gate: asyncio.Event | None):
        self._lines, self._gate = list(lines), gate

    async def readline(self) -> bytes:
        if self._gate is not None:
            await self._gate.wait()
            await asyncio.sleep(0.3)
        return self._lines.pop(0) if self._lines else b""

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line


class _Stdin:
    def __init__(self, proc):
        self._proc = proc

    def write(self, data: bytes) -> None:
        self._proc.stdin_bytes += data
        if self._proc.gate is not None:
            self._proc.gate.set()

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Stderr:
    async def read(self) -> bytes:
        return b""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _Proc:
    def __init__(self, lines: list[bytes], gated: bool):
        self.gate = asyncio.Event() if gated else None
        self.stdin_bytes = b""
        self.stdin = _Stdin(self)
        self.stdout = _Stdout(lines, self.gate)
        self.stderr = _Stderr()
        # A per-turn harness has exited by the time it is reaped, so a reaper
        # signals nothing; claude's process lives across turns.
        self.returncode = None if gated else 0
        self.pid = NO_SUCH_PID

    async def wait(self) -> int:
        return 0

    def send_signal(self, sig) -> None:
        return None


def _line(event: dict) -> bytes:
    return json.dumps(event).encode() + b"\n"


#: What each harness writes for one finished turn.
TURN_OUTPUT = {
    "claude": [_line({"type": "assistant", "message": {"content": [
                   {"type": "text", "text": "hi"}]}}),
               _line({"type": "result", "session_id": "conv-1"})],
    "codex": [_line({"type": "thread.started", "thread_id": "thread-1"}),
              _line({"type": "item.completed",
                     "item": {"type": "agent_message", "text": "hi"}}),
              _line({"type": "turn.completed"})],
    "dsh": [_line({"sessionId": "conv-1", "outcome": "completed", "text": "hi",
                   "traffic": {}})],
}


class _Spawner:
    """``asyncio.create_subprocess_exec``, recording what each spawn was."""

    def __init__(self, harness: str, outputs: list[list[bytes]] | None = None):
        self.harness = harness
        self.outputs = list(outputs or [])
        self.calls: list[dict] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> _Proc:
        call = {"argv": list(argv), "env": kwargs.get("env") or {}}
        if "--task-file" in argv:
            # dsh's turn arrives in a file the adapter deletes afterwards.
            call["task"] = Path(argv[argv.index("--task-file") + 1]).read_text()
        lines = self.outputs.pop(0) if self.outputs else TURN_OUTPUT[self.harness]
        proc = _Proc(lines, gated=self.harness == "claude")
        call["proc"] = proc
        self.calls.append(call)
        return proc


def _delivered(harness: str, call: dict) -> str:
    """The user turn a harness was actually handed, by whichever channel."""
    if harness == "claude":
        message = json.loads(call["proc"].stdin_bytes.decode().splitlines()[-1])
        return message["message"]["content"][0]["text"]
    if harness == "codex":
        return call["proc"].stdin_bytes.decode()
    return call["task"]


# ---------------------------------------------------------------------------
# 15. /models for a named harness relays that harness's listing
# ---------------------------------------------------------------------------

def test_models_for_a_named_harness_relays_that_harnesss_listing_dsh_included(homes, proxy):
    wire = proxy({
        "claude": [{"id": "claude-opus-5"}],
        "dsh": [{"id": "qwen35-131k", "context_window": 131072,
                 "effort_levels": ["low", "high"]}],
    })
    client = TestClient(mind_server.app)

    resp = client.get("/models?harness=dsh", headers=ADMIN)

    assert resp.status_code == 200
    assert wire.seen == ["http://proxy.test/v1/models?harness=dsh"]
    assert [(m["name"], m["context_window"], m["effort_levels"])
            for m in resp.json()["models"]] == [("qwen35-131k", 131072, ["low", "high"])]


# ---------------------------------------------------------------------------
# 16. A harness is offered only with its CLI, hooks and login present
# ---------------------------------------------------------------------------

def _executable(path: Path) -> None:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


def test_a_harness_is_offered_only_with_its_cli_hooks_and_login(homes, monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                "OPENAI_API_KEY", "DSH_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "claude")
    monkeypatch.setenv("PATH", str(bin_dir))
    hooks = {"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "bash hooks/auto_remember.sh"},
                            {"type": "command", "command": "bash hooks/rotation_check.sh"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command",
                                         "command": "bash hooks/surface_inject.sh"}]}],
    }}
    # claude: everything present.
    (homes["claude"] / "settings.json").write_text(json.dumps(hooks))
    (homes["claude"] / ".credentials.json").write_text("{}")
    # codex: logged in, but no CLI and no Stop hooks.
    (homes["codex"] / "auth.json").write_text("{}")
    (homes["codex"] / "config.toml").write_text(
        '[[hooks.UserPromptSubmit]]\n[[hooks.UserPromptSubmit.hooks]]\n'
        'type = "command"\ncommand = "bash time_inject.sh"\n')
    # dsh: launcher and hooks present, no proxy key to log in with.
    launcher = tmp_path / "dsh-bin.js"
    launcher.write_text("")
    monkeypatch.setattr(dsh_cli, "DSH_BIN", str(launcher))
    dsh_hooks = tmp_path / "dsh-hooks.json"
    dsh_hooks.write_text(json.dumps(hooks))
    monkeypatch.setenv("DSH_HOOKS_CONFIG", str(dsh_hooks))

    resp = TestClient(mind_server.app).get("/harnesses", headers=ADMIN)

    assert resp.status_code == 200
    body = resp.json()
    rows = {row["name"]: row for row in body["harnesses"]}
    assert rows["claude"] == {"name": "claude", "available": True, "reason": ""}
    assert rows["codex"]["available"] is False
    assert "codex CLI not on PATH" in rows["codex"]["reason"]
    assert "auto_remember" in rows["codex"]["reason"]
    assert "rotation_check" in rows["codex"]["reason"]
    assert "login" not in rows["codex"]["reason"]
    assert rows["dsh"]["available"] is False
    assert "proxy key" in rows["dsh"]["reason"]
    assert body["default"] == "claude"


def test_the_harness_report_refuses_a_caller_without_the_admin_token(homes):
    assert TestClient(mind_server.app).get("/harnesses", headers=SESSION).status_code == 401


# ---------------------------------------------------------------------------
# 17. One server runs a chat session on whichever adapter the harness names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_a_chat_session_runs_on_the_adapter_its_harness_names(
    homes, proxy, monkeypatch, harness,
):
    proxy({})
    spawner = _Spawner(harness)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    client = TestClient(mind_server.app)

    created = client.post("/sessions", headers=SESSION, json={
        "session_id": "row-1", "resume_sid": "conv-1", "model": "some-model",
        "harness": harness, "system_prompt_blocks": "SOUL",
        "opening_turn": "HANDOVER",
    })
    assert created.status_code == 200, created.text
    reply = client.post("/sessions/row-1/message", headers=SESSION,
                        json={"content": "hello"})

    assert reply.status_code == 200
    assert '"is_error": false' in reply.text or '"result"' in reply.text
    expected_cli = {"claude": "claude", "codex": "codex", "dsh": dsh_cli.DSH_BIN}[harness]
    assert spawner.calls[0]["argv"][0] == expected_cli
    # The handover is the front of the first user turn, on every harness.
    assert _delivered(harness, spawner.calls[-1]).endswith("HANDOVER\n\n---\n\nhello")
    # The other adapters were never asked to hold it.
    others = [a for n, a in mind_server.ADAPTERS.items() if n != harness]
    assert all("row-1" not in adapter.SESSIONS for adapter in others)


def test_the_handover_is_delivered_once_not_on_every_turn(homes, proxy, monkeypatch):
    proxy({})
    spawner = _Spawner("codex")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    client = TestClient(mind_server.app)
    client.post("/sessions", headers=SESSION, json={
        "session_id": "row-2", "resume_sid": "conv-2", "model": "gpt-5",
        "harness": "codex", "opening_turn": "HANDOVER",
    })

    client.post("/sessions/row-2/message", headers=SESSION, json={"content": "one"})
    client.post("/sessions/row-2/message", headers=SESSION, json={"content": "two"})

    assert _delivered("codex", spawner.calls[1]) == "two"


def test_a_session_with_no_harness_runs_on_the_minds_default(homes, proxy, monkeypatch):
    proxy({})
    spawner = _Spawner("claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)

    TestClient(mind_server.app).post("/sessions", headers=SESSION, json={
        "session_id": "row-3", "resume_sid": "conv-3", "model": "claude-opus-5",
    })

    assert spawner.calls[0]["argv"][0] == "claude"
    assert "row-3" in claude_cli.SESSIONS


# ---------------------------------------------------------------------------
# 18. A terminal attach opens the harness's CLI, with the stored handover
# ---------------------------------------------------------------------------

def _echo_client(**kwargs):
    """A pty running `cat`, standing in for a tmux client."""
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    proc = subprocess.Popen(["cat"], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd)
    os.close(slave_fd)
    return proc, master_fd


@pytest.fixture
def panes(monkeypatch, homes, proxy):
    """Every adapter's tmux server, recording what its panes run."""
    proxy({})
    seen: dict[str, dict] = {}
    live: set[tuple[str, str]] = set()
    for name, adapter in mind_server.ADAPTERS.items():
        record = seen.setdefault(name, {"start": [], "respawn": [], "killed": []})

        def start(sid, argv, *, env_overrides, cols, rows, _r=record, _n=name):
            _r["start"].append({"argv": argv, "env": env_overrides})
            live.add((_n, sid))

        def respawn(sid, argv, *, env_overrides, _r=record):
            _r["respawn"].append({"argv": argv, "env": env_overrides})

        def kill(sid, _r=record, _n=name):
            _r["killed"].append(sid)
            live.discard((_n, sid))
            return True

        monkeypatch.setattr(adapter.TERMINALS, "start", start)
        monkeypatch.setattr(adapter.TERMINALS, "respawn", respawn)
        monkeypatch.setattr(adapter.TERMINALS, "kill", kill)
        monkeypatch.setattr(adapter.TERMINALS, "alive",
                            lambda sid, _n=name: (_n, sid) in live)
        monkeypatch.setattr(adapter.TERMINALS, "attach",
                            lambda sid, **kw: _echo_client(**kw))
    monkeypatch.setattr(codex_cli, "_watch_for_new_thread_in_background",
                        lambda *a, **k: None)
    return seen


def _attach(client, sid, harness, model, carried, monkeypatch):
    async def fetch(session_id, claude_sid):
        return carried

    monkeypatch.setattr(pty_attach, "fetch_carry_forward", fetch)
    with client.websocket_connect(
        f"/sessions/{sid}/attach-pty?harness={harness}&model={model}&resume_sid=conv-{sid}",
        headers=SESSION,
    ) as ws:
        ws.send_bytes(b"x\n")
        ws.receive_bytes()


def _seed_in(argv: list[str]) -> Path:
    """The seed file a seeded pane's shell reads, from its command."""
    script = argv[2]
    return Path(script.split("seed=$(cat ", 1)[1].split(" ", 1)[0].strip("'"))


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_an_attach_opens_the_named_harness_with_the_handover_as_its_first_turn(
    panes, monkeypatch, harness,
):
    client = TestClient(mind_server.app)

    _attach(client, "p1", harness, "m1", "HANDOVER TEXT", monkeypatch)

    started = panes[harness]["start"][0]["argv"]
    assert started[:2] == ["/bin/sh", "-c"]
    assert f"exec {harness} " in started[2]
    # A user turn, not standing context: a system prompt reaches no
    # transcript, and the next switch would find the handover gone.
    assert "--append-system-prompt" not in started[2]
    assert _seed_in(started).read_text() == "HANDOVER TEXT"
    assert all(not panes[n]["start"] for n in panes if n != harness)


def test_a_dsh_attach_answers_the_handover_as_its_first_turn(panes, monkeypatch):
    client = TestClient(mind_server.app)

    _attach(client, "p2", "dsh", "qwen35-131k", "HANDOVER TEXT", monkeypatch)

    argv = panes["dsh"]["start"][0]["argv"]
    assert argv[0] == dsh_cli.DSH_BIN
    assert "--context-as-turn" in argv
    assert Path(argv[argv.index("--context-file") + 1]).read_text() == "HANDOVER TEXT"


def test_a_switched_conversations_next_attach_opens_the_new_harness(panes, monkeypatch):
    client = TestClient(mind_server.app)
    _attach(client, "p3", "claude", "claude-opus-5", "", monkeypatch)

    _attach(client, "p3", "codex", "gpt-5", "HANDOVER TEXT", monkeypatch)

    assert panes["claude"]["killed"] == ["p3"]
    assert "exec codex " in panes["codex"]["start"][0]["argv"][2]
    assert pty_attach.PTYS["p3"].harness == "codex"


# ---------------------------------------------------------------------------
# 19. A codex pane runs the model it is handed, not a cached one
# ---------------------------------------------------------------------------

def test_a_codex_pane_runs_the_model_it_is_handed_not_a_cached_one(panes, monkeypatch):
    client = TestClient(mind_server.app)
    _attach(client, "p4", "codex", "gpt-old", "", monkeypatch)
    # The conversation's model changed; the tile reattaches naming the new one.
    _attach(client, "p4", "codex", "gpt-new", "", monkeypatch)

    unnamed = client.post("/sessions/p4/rotate-pty", headers=SESSION, json={
        "new_claude_sid": "conv-r1", "harness": "codex",
    })
    named = client.post("/sessions/p4/rotate-pty", headers=SESSION, json={
        "new_claude_sid": "conv-r2", "harness": "codex", "model": "gpt-named",
        "effort": "high",
    })

    assert unnamed.json()["rotated"] is True
    assert named.json()["rotated"] is True
    first, second = (" ".join(r["argv"]) for r in panes["codex"]["respawn"])
    assert "--model gpt-new" in first and "gpt-old" not in first
    assert "--model gpt-named" in second
    assert 'model_reasoning_effort="high"' in second


def test_a_rotation_addressed_to_another_harness_leaves_the_pane_alone(panes, monkeypatch):
    client = TestClient(mind_server.app)
    _attach(client, "p5", "claude", "claude-opus-5", "", monkeypatch)

    resp = client.post("/sessions/p5/rotate-pty", headers=SESSION, json={
        "new_claude_sid": "conv-r3", "harness": "codex", "model": "gpt-5",
    })

    assert resp.status_code == 409
    assert resp.json()["rotated"] is False
    assert panes["claude"]["respawn"] == [] and panes["codex"]["respawn"] == []


# ---------------------------------------------------------------------------
# 20. The default harness is written with a model it offers, or not at all
# ---------------------------------------------------------------------------

def _runtime_app(tmp_path: Path) -> tuple[TestClient, Path]:
    path = tmp_path / "runtime.yaml"
    path.write_text(
        "# A mind.\nname: m\nmind_id: m-1\ngateway_url: http://m:8420\n"
        "harness: claude_cli\nprovider: anthropic\n"
        "# The model every new conversation starts on.\ndefault_model: claude-opus-5\n"
    )
    app = FastAPI()
    runtime_api.install_runtime_routes(app, path=path, mind_id="m-1",
                                       log=mind_server.log)
    return TestClient(app), path


def test_setting_the_default_harness_with_a_model_it_offers_writes_both(
    tmp_path, proxy, monkeypatch,
):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    proxy({"codex": [{"id": "gpt-5.6-terra", "context_window": 400_000}]})
    client, path = _runtime_app(tmp_path)

    resp = client.patch("/runtime", headers=ADMIN,
                        json={"harness": "codex", "default_model": "gpt-5.6-terra"})

    assert resp.status_code == 200, resp.text
    written = yaml.safe_load(path.read_text())
    assert (written["harness"], written["default_model"]) == ("codex", "gpt-5.6-terra")
    assert written["model_context_window"] == 400_000
    assert "# The model every new conversation starts on." in path.read_text()


@pytest.mark.parametrize("body", [
    {"harness": "codex", "default_model": "claude-opus-5"},
    {"harness": "codex"},
    {"harness": "hermes", "default_model": "gpt-5.6-terra"},
])
def test_a_default_harness_without_a_model_it_offers_is_refused_and_nothing_written(
    tmp_path, proxy, monkeypatch, body,
):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    proxy({"codex": [{"id": "gpt-5.6-terra"}], "claude": [{"id": "claude-opus-5"}]})
    client, path = _runtime_app(tmp_path)
    before = path.read_bytes()

    resp = client.patch("/runtime", headers=ADMIN, json=body)

    assert resp.status_code in (400, 409)
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# 21. The rotation threshold a conversation runs under is its model's own
# ---------------------------------------------------------------------------

def test_a_conversations_rotation_threshold_follows_its_own_models_window(
    homes, proxy, monkeypatch, tmp_path,
):
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        "name: example\nmind_id: example\nharness: claude_cli\n"
        "default_model: claude-sonnet-5\nmodel_context_window: 200000\n"
        "rotation_threshold_percent: 10\n"
    )
    monkeypatch.setattr(mind_server, "RUNTIME_PATH", runtime)
    proxy({"claude": [{"id": "claude-sonnet-5", "context_window": 200_000},
                      {"id": "claude-opus-5", "context_window": 1_000_000}],
           "dsh": [{"id": "qwen35-131k", "context_window": 131_072}]})
    spawner = _Spawner("claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    client = TestClient(mind_server.app)

    client.post("/sessions", headers=SESSION, json={
        "session_id": "t1", "resume_sid": "conv-t1", "model": "claude-opus-5",
        "harness": "claude",
    })

    env = spawner.calls[0]["env"]
    assert env["HIVE_MODEL_CONTEXT_WINDOW"] == str(1_000_000)
    assert env["HIVE_ROTATION_THRESHOLD_TOKENS"] == str(1_000_000 * 10 // 100)


def test_a_dsh_pane_on_a_mind_with_no_serving_ceiling_is_sized_by_its_model(
    panes, monkeypatch, proxy,
):
    """A claude mind's file has no `context_window`; its dsh conversation
    runs at its model's own window rather than refusing to start."""
    proxy({"dsh": [{"id": "qwen35-131k", "context_window": 131_072}]})
    monkeypatch.delitem(dsh_cli.RUNTIME, "context_window", raising=False)

    _attach(TestClient(mind_server.app), "p6", "dsh", "qwen35-131k", "", monkeypatch)

    assert panes["dsh"]["start"][0]["env"]["DSH_MODEL_CONTEXT_WINDOW"] == "131072"


# ---------------------------------------------------------------------------
# The handover route and the switch's kill
# ---------------------------------------------------------------------------

def _handover(client, **body):
    return client.post("/handover", headers=ADMIN, json={
        "summary": "", "harness_sid": None, "budget_bytes": 120_000, **body,
    })


def test_a_handover_reads_the_old_harnesss_own_transcript(homes):
    target = pty_attach.claude_transcript_path("conv-h1", claude_cli.PROJECT_DIR,
                                               homes["claude"])
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude.jsonl", target)

    resp = _handover(TestClient(mind_server.app), harness="claude", claude_sid="conv-h1",
                     summary="We were removing a duplicate discord.", budget_bytes=40_000)

    assert resp.status_code == 200
    text = resp.json()["text"]
    assert text.startswith("Summary of the conversation so far:\n"
                           "We were removing a duplicate discord.")
    assert "User: hey, i have two versions of discord installed" in text
    assert len(text.encode()) <= 40_000


def test_a_codex_handover_is_found_by_the_thread_the_gateway_holds(homes):
    rollouts = homes["codex"] / "sessions" / "2026" / "06" / "01"
    rollouts.mkdir(parents=True)
    shutil.copy(FIXTURES / CODEX_ROLLOUT, rollouts / CODEX_ROLLOUT)

    resp = _handover(TestClient(mind_server.app), harness="codex", claude_sid="conv-h2",
                     harness_sid=CODEX_THREAD)

    assert "Tool call (exec_command)" in resp.json()["text"]


def test_an_unreadable_transcript_with_no_summary_refuses_the_handover(homes):
    log_dir = (homes["dsh"] / "sessions" / dsh_cli._project_key(dsh_cli._spawn_cwd())
               / dsh_cli._encode_segment("conv-h3"))
    log_dir.mkdir(parents=True)
    (log_dir / "session.jsonl.zstd").write_bytes(b"not a zstd frame at all")
    client = TestClient(mind_server.app)

    refused = _handover(client, harness="dsh", claude_sid="conv-h3")
    summarised = _handover(client, harness="dsh", claude_sid="conv-h3", summary="the gist")

    assert (refused.status_code, refused.json()) == (422, {"detail": "unreadable"})
    assert summarised.json() == {"text": "Summary of the conversation so far:\nthe gist"}


def test_a_conversation_that_never_had_a_turn_hands_over_nothing(homes):
    """No transcript on disk is an empty conversation, not an unreadable one:
    a conversation must be switchable before its first turn."""
    resp = _handover(TestClient(mind_server.app), harness="claude", claude_sid="conv-new")

    assert (resp.status_code, resp.json()) == (200, {"text": ""})


def test_a_switch_kill_forgets_the_codex_thread_and_a_plain_kill_keeps_it(homes):
    client = TestClient(mind_server.app)
    codex_cli.THREADS["k1"] = "thread-k1"

    client.delete("/sessions/k1", headers=SESSION)
    kept = dict(codex_cli.THREADS)
    client.delete("/sessions/k1?forget_thread=1", headers=SESSION)

    assert kept == {"k1": "thread-k1"}
    assert "k1" not in codex_cli.THREADS



# ---------------------------------------------------------------------------
# Grill round 1
# ---------------------------------------------------------------------------

FAILED_CODEX_TURN = [_line({"type": "thread.started", "thread_id": "thread-f"}),
                     _line({"type": "turn.failed", "error": {"message": "boom"}})]


def test_the_handover_is_held_until_a_turn_completes_without_error(homes, proxy, monkeypatch):
    """A failed first turn must not spend the handover: codex starts a fresh
    thread after a failure, and that thread would open on nothing."""
    proxy({})
    spawner = _Spawner("codex", outputs=[FAILED_CODEX_TURN])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    client = TestClient(mind_server.app)
    client.post("/sessions", headers=SESSION, json={
        "session_id": "g1", "resume_sid": "conv-g1", "model": "gpt-5",
        "harness": "codex", "system_prompt_blocks": "SOUL", "opening_turn": "HANDOVER",
    })

    for content in ("one", "two", "three"):
        client.post("/sessions/g1/message", headers=SESSION, json={"content": content})

    delivered = [_delivered("codex", call) for call in spawner.calls]
    assert delivered[0].endswith("HANDOVER\n\n---\n\none")
    assert delivered[1].endswith("HANDOVER\n\n---\n\ntwo")
    assert delivered[2] == "three"


def test_a_resumed_codex_thread_also_gets_the_held_opening_turn(homes, proxy, monkeypatch):
    proxy({})
    spawner = _Spawner("codex")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    codex_cli.THREADS["g2"] = "thread-existing"
    client = TestClient(mind_server.app)
    client.post("/sessions", headers=SESSION, json={
        "session_id": "g2", "resume_sid": "conv-g2", "model": "gpt-5",
        "harness": "codex", "opening_turn": "HANDOVER",
    })

    client.post("/sessions/g2/message", headers=SESSION, json={"content": "hello"})

    assert spawner.calls[0]["argv"][-3:] == ["resume", "thread-existing", "-"]
    assert _delivered("codex", spawner.calls[0]) == "HANDOVER\n\n---\n\nhello"


def test_a_missing_transcript_after_turns_with_no_summary_is_unreadable(homes):
    resp = _handover(TestClient(mind_server.app), harness="claude", claude_sid="conv-gone",
                     had_turns=True)

    assert (resp.status_code, resp.json()) == (422, {"detail": "unreadable"})


def test_a_codex_handover_without_a_thread_id_uses_the_minds_own_thread(homes):
    rollouts = homes["codex"] / "sessions" / "2026" / "06" / "01"
    rollouts.mkdir(parents=True)
    shutil.copy(FIXTURES / CODEX_ROLLOUT, rollouts / CODEX_ROLLOUT)
    codex_cli.THREADS["g3"] = CODEX_THREAD

    resp = _handover(TestClient(mind_server.app), harness="codex", claude_sid="conv-g3",
                     session_id="g3", had_turns=True)

    assert "Tool call (exec_command)" in resp.json()["text"]


def test_a_handover_honours_the_budget_it_is_given(homes):
    log_dir = (homes["dsh"] / "sessions" / dsh_cli._project_key(dsh_cli._spawn_cwd())
               / dsh_cli._encode_segment("conv-g4"))
    log_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "dsh" / "session.jsonl.zstd", log_dir / "session.jsonl.zstd")

    text = _handover(TestClient(mind_server.app), harness="dsh", claude_sid="conv-g4",
                     budget_bytes=1_500).json()["text"]

    assert 0 < len(text.encode("utf-8")) <= 1_500


def test_an_attach_tears_down_an_idle_chat_process_first(panes, monkeypatch):
    spawner = _Spawner("claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    client = TestClient(mind_server.app)
    client.post("/sessions", headers=SESSION, json={
        "session_id": "g5", "resume_sid": "conv-g5", "model": "claude-opus-5",
        "harness": "claude",
    })
    assert "g5" in claude_cli.SESSIONS

    _attach(client, "g5", "claude", "claude-opus-5", "", monkeypatch)

    assert "g5" not in claude_cli.SESSIONS
    assert panes["claude"]["start"]


def test_a_later_rotation_without_a_model_keeps_the_last_named_model_and_effort(
    panes, monkeypatch,
):
    client = TestClient(mind_server.app)
    _attach(client, "g6", "codex", "gpt-a", "", monkeypatch)
    client.post("/sessions/g6/rotate-pty", headers=SESSION, json={
        "new_claude_sid": "r1", "harness": "codex", "model": "gpt-b", "effort": "high"})

    client.post("/sessions/g6/rotate-pty", headers=SESSION, json={
        "new_claude_sid": "r2", "harness": "codex"})

    last = " ".join(panes["codex"]["respawn"][-1]["argv"])
    assert "--model gpt-b" in last
    assert 'model_reasoning_effort="high"' in last


def test_the_next_attach_on_another_harness_is_cold_on_one_shared_tmux_session(
    homes, proxy, monkeypatch,
):
    """Production runs every harness's panes on one tmux socket under one
    session name, so the old pane answers `alive` for the new harness too."""
    proxy({})
    live: set[str] = set()
    started: dict[str, list] = {"claude": [], "codex": []}
    fetched: list[str] = []
    for name in ("claude", "codex", "dsh"):
        terminals = mind_server.ADAPTERS[name].TERMINALS
        monkeypatch.setattr(terminals, "start",
                            lambda sid, argv, _n=name, **kw: (started.setdefault(_n, []).append(argv),
                                                              live.add(sid)))
        monkeypatch.setattr(terminals, "kill", lambda sid: (live.discard(sid), True)[1])
        monkeypatch.setattr(terminals, "alive", lambda sid: sid in live)
        monkeypatch.setattr(terminals, "attach", lambda sid, **kw: _echo_client(**kw))
    monkeypatch.setattr(codex_cli, "_watch_for_new_thread_in_background", lambda *a, **k: None)
    client = TestClient(mind_server.app)

    async def fetch(session_id, claude_sid):
        fetched.append(claude_sid)
        return "HANDOVER TEXT"

    for harness in ("claude", "codex"):
        monkeypatch.setattr(pty_attach, "fetch_carry_forward", fetch)
        with client.websocket_connect(
            f"/sessions/g7/attach-pty?harness={harness}&model=m&resume_sid=conv-{harness}",
            headers=SESSION,
        ) as ws:
            ws.send_bytes(b"x\n")
            ws.receive_bytes()

    assert fetched == ["conv-claude", "conv-codex"]
    assert _seed_in(started["codex"][0]).read_text() == "HANDOVER TEXT"


def _all_hooks() -> dict:
    return {"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "auto_remember.sh"},
                            {"type": "command", "command": "rotation_check.sh"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "surface_inject.sh"}]}],
    }}


@pytest.fixture
def equipped(homes, monkeypatch, tmp_path):
    """Every harness with its CLI, hooks and login present."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                "OPENAI_API_KEY", "DSH_API_KEY", "OLLAMA_BASE_URL", "OPENAI_BASE_URL",
                "ANTHROPIC_BASE_URL", "DSH_PROXY_BASE_URL", "DSH_HOOKS_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for cli in ("claude", "codex"):
        _executable(bin_dir / cli)
    monkeypatch.setenv("PATH", str(bin_dir))
    launcher = tmp_path / "dsh-bin.js"
    launcher.write_text("")
    monkeypatch.setattr(dsh_cli, "DSH_BIN", str(launcher))
    (homes["claude"] / "settings.json").write_text(json.dumps(_all_hooks()))
    (homes["claude"] / ".credentials.json").write_text("{}")
    (homes["codex"] / "config.toml").write_text(
        '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "auto_remember.sh"\n'
        '[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "rotation_check.sh"\n'
        '[[hooks.UserPromptSubmit]]\n[[hooks.UserPromptSubmit.hooks]]\n'
        'type = "command"\ncommand = "surface_inject.sh"\n')
    (homes["codex"] / "auth.json").write_text("{}")
    # dsh's hooks resolve under DSH_HOME, the way its adapter resolves it.
    (homes["dsh"] / "hooks.json").write_text(json.dumps(_all_hooks()))
    monkeypatch.setenv("DSH_API_KEY", "hmp-dsh")
    monkeypatch.setenv("DSH_PROXY_BASE_URL", "http://proxy.test")
    return homes


def _report() -> dict[str, dict]:
    body = TestClient(mind_server.app).get("/harnesses", headers=ADMIN).json()
    return {row["name"]: row for row in body["harnesses"]}


def test_every_harness_is_offered_when_fully_equipped(equipped):
    assert all(row["available"] for row in _report().values()), _report()


@pytest.mark.parametrize("harness,remove", [
    ("claude", lambda h: (h["claude"] / ".credentials.json").unlink()),
    ("codex", lambda h: (h["codex"] / "auth.json").unlink()),
    ("dsh", lambda h: os.environ.pop("DSH_API_KEY")),
    ("dsh", lambda h: os.environ.pop("DSH_PROXY_BASE_URL")),
])
def test_a_harness_without_its_login_is_not_offered(equipped, harness, remove):
    remove(equipped)

    row = _report()[harness]

    assert row["available"] is False
    assert "login" in row["reason"] or "proxy" in row["reason"]


def test_a_harness_without_a_user_prompt_hook_is_not_offered(equipped):
    hooks = _all_hooks()
    del hooks["hooks"]["UserPromptSubmit"]
    (equipped["claude"] / "settings.json").write_text(json.dumps(hooks))

    row = _report()["claude"]

    assert row["available"] is False and "UserPromptSubmit" in row["reason"]


def test_a_harness_whose_adapter_failed_to_load_is_not_offered(equipped, monkeypatch):
    loaded, failed = mind_server.load_adapters(
        {"claude": "minds.harness.claude_cli", "dsh": "minds.harness.no_such_adapter"})
    monkeypatch.setattr(mind_server, "ADAPTERS", loaded)
    monkeypatch.setattr(mind_server, "LOAD_FAILURES", failed)

    rows = _report()

    assert rows["claude"]["available"] is True
    assert rows["dsh"]["available"] is False
    assert "failed to load" in rows["dsh"]["reason"]


def test_a_dsh_launch_carries_the_agents_patch_only_when_it_exists(homes, proxy, monkeypatch):
    proxy({})
    spawner = _Spawner("dsh")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    client = TestClient(mind_server.app)
    client.post("/sessions", headers=SESSION, json={
        "session_id": "g8", "resume_sid": "conv-g8", "model": "qwen", "harness": "dsh"})

    client.post("/sessions/g8/message", headers=SESSION, json={"content": "one"})
    patch = homes["dsh"] / "agents.patch.yml"
    patch.write_text("agents: []\n")
    client.post("/sessions/g8/message", headers=SESSION, json={"content": "two"})

    assert "--patch" not in spawner.calls[0]["argv"]
    argv = spawner.calls[1]["argv"]
    assert argv[argv.index("--patch") + 1] == str(patch)


def test_the_skills_check_runs_the_render_pass(homes, monkeypatch):
    ran: list[bool] = []
    monkeypatch.setattr(mind_server.skills_api, "check_all",
                        lambda **kw: ran.append(True) or {"rendered": 3}, raising=False)

    resp = TestClient(mind_server.app).post("/skills/check", headers=ADMIN)

    assert (resp.status_code, resp.json(), ran) == (200, {"rendered": 3}, [True])


def test_the_skills_check_is_refused_rather_than_faked_without_the_render_pass(
    homes, monkeypatch,
):
    monkeypatch.delattr(mind_server.skills_api, "check_all", raising=False)

    resp = TestClient(mind_server.app).post("/skills/check", headers=ADMIN)

    assert resp.status_code == 501


def test_the_skills_page_answers_for_the_harness_it_names(homes, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(mind_server.skills_api, "list_skills",
                        lambda harness: seen.append(harness) or [])

    client = TestClient(mind_server.app)
    client.get("/skills?harness=codex", headers=ADMIN)
    client.get("/skills", headers=ADMIN)

    assert [name.removesuffix("_cli") for name in seen] == ["codex", "claude"]
