"""A Claude or Codex mind is started under the role guard (feature 10.18,
stories 10.18.6 and 10.18.7).

Protected behaviour 1 of Role Management, on these two harnesses: a mind
holding roles can use exactly the built-in tools their union grants. The
guard itself is tested in `test_role_guard.py`; what is asserted here is that
the process each adapter really starts is handed it — the hook command taken
out of the argv the adapter built is run as the harness would run it, with
the environment the adapter built, and its exit code is what refuses a tool.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import minds.harness.claude_cli as claude
import minds.harness.codex_cli as codex
from minds.harness import role_guard


def _managed(*tools):
    return {"managed": True, "roles": ["general", "writer"], "skills": [], "hooks": [],
            "models": [], "tools": list(tools)}


def _says(monkeypatch, grants):
    """What the gateway answers at spawn. The hook itself, run below with no
    gateway to reach, judges by the grants that spawn wrote."""
    monkeypatch.setattr(role_guard, "fetch", lambda *a, **k: grants)


def _hook(command, tool, env):
    env = {**env, "COMMS_URL": ""}
    event = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}
    return subprocess.run(command, shell=True, input=json.dumps(event), env=env,
                          capture_output=True, text=True, timeout=30).returncode


def _claude_guard(argv):
    settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
    return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def _codex_guard(argv):
    override = next(argv[i + 1] for i, word in enumerate(argv)
                    if word == "-c" and argv[i + 1].startswith("hooks.PreToolUse="))
    groups = tomllib.loads("v = " + override.split("=", 1)[1])["v"]
    return groups[0]["hooks"][0]["command"]


@pytest.fixture()
def claude_home(monkeypatch, tmp_path):
    monkeypatch.setattr(claude, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(claude, "ensure_tui_first_run_flags", lambda *a, **k: None)

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(claude, "_drain_stderr", nothing)
    return tmp_path


def _spawn_claude(started):
    return claude._spawn_proc("s1", model="opus", resume_sid=None, surface_prompt=None,
                              allowed_directories=None)


async def test_a_claude_chat_process_is_started_under_the_guard(monkeypatch, claude_home):
    _says(monkeypatch, _managed("files"))
    started = AsyncMock(return_value=MagicMock(pid=42))
    monkeypatch.setattr(claude.asyncio, "create_subprocess_exec", started)

    await _spawn_claude(started)
    guard, env = _claude_guard(list(started.call_args.args)), started.call_args.kwargs["env"]

    assert _hook(guard, "Bash", env) == role_guard.DENY
    assert _hook(guard, "Read", env) == 0


async def test_a_claude_process_the_gateway_cannot_answer_for_is_not_started(
        monkeypatch, claude_home):
    _says(monkeypatch, None)
    started = AsyncMock(return_value=MagicMock(pid=42))
    monkeypatch.setattr(claude.asyncio, "create_subprocess_exec", started)

    with pytest.raises(RuntimeError, match="not started"):
        await _spawn_claude(started)

    assert started.await_count == 0


def _pane(monkeypatch, harness):
    """Start a pane with tmux faked out; return its argv and environment."""
    started = {}
    monkeypatch.setattr(harness.TERMINALS, "alive", lambda sid: False)
    monkeypatch.setattr(harness.TERMINALS, "start",
                        lambda sid, argv, **kw: started.update(argv=argv, env=kw["env_overrides"]))
    monkeypatch.setattr(harness.TERMINALS, "attach",
                        lambda sid, **kw: (types.SimpleNamespace(pid=1, poll=lambda: None), -1))
    harness._spawn_pty(session_id="p1", model="opus", conversation_id="conv-9",
                       cols=80, rows=24)
    return started["argv"], {"PATH": "/usr/bin:/bin", **started["env"]}


def _pane_program(argv):
    """The harness's own argv out of the `sh -c` a seeded pane is wrapped in."""
    import shlex
    return shlex.split(argv[-1]) if argv[0] in ("sh", "bash") else list(argv)


def test_a_claude_terminal_pane_is_started_under_the_guard(monkeypatch, claude_home):
    _says(monkeypatch, _managed("shell"))

    argv, env = _pane(monkeypatch, claude)
    guard = _claude_guard(_pane_program(argv))

    assert _hook(guard, "WebFetch", env) == role_guard.DENY
    assert _hook(guard, "Bash", env) == 0


@pytest.fixture()
def codex_home(monkeypatch, tmp_path):
    monkeypatch.setattr(codex, "CODEX_HOME", tmp_path)
    monkeypatch.setattr(codex, "_watch_for_new_thread_in_background", lambda *a, **k: None)
    codex.SESSIONS.clear()
    codex.THREADS.clear()
    yield tmp_path
    codex.SESSIONS.clear()
    codex.THREADS.clear()


class _Lines:
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def read(self):
        return b""


class _Proc:
    pid = 7
    returncode = 0

    def __init__(self):
        self.stdin = MagicMock(drain=AsyncMock())
        self.stdout = _Lines([json.dumps({"type": "turn.completed"}).encode() + b"\n"])
        self.stderr = _Lines([])

    async def wait(self):
        return 0


async def _codex_turn(monkeypatch, started):
    async def fake_exec(*cmd, **kwargs):
        started.append((list(cmd), kwargs["env"]))
        return _Proc()

    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", fake_exec)
    codex.SESSIONS["s1"] = {"system_prompt": "system", "thread_id": None, "model": "gpt-5"}
    return [event async for event in codex._run_codex_turn("s1", "hello", None)]


async def test_a_codex_turn_is_started_under_the_guard(monkeypatch, codex_home):
    _says(monkeypatch, _managed("shell"))
    started: list = []

    await _codex_turn(monkeypatch, started)
    argv, env = started[0]
    guard = _codex_guard(argv)

    assert _hook(guard, "apply_patch", env) == role_guard.DENY
    assert _hook(guard, "Bash", env) == 0
    assert 'web_search="disabled"' in argv
    assert argv[-1] == "-" and argv[:2] == ["codex", "exec"]


async def test_a_codex_turn_the_gateway_cannot_answer_for_says_so_and_is_not_started(
        monkeypatch, codex_home):
    _says(monkeypatch, None)
    started: list = []

    events = await _codex_turn(monkeypatch, started)

    assert started == []
    assert events[-1]["is_error"] is True and "not started" in events[-1]["errors"][0]


def test_a_codex_terminal_pane_is_started_under_the_guard(monkeypatch, codex_home):
    _says(monkeypatch, _managed("files"))

    argv, env = _pane(monkeypatch, codex)
    guard = _codex_guard(_pane_program(argv))

    assert _hook(guard, "Bash", env) == role_guard.DENY
    assert _hook(guard, "apply_patch", env) == 0
