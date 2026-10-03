"""The dsh harness adapter: one process per turn, in the gateway's conversation.

Every test imports ``minds.harness.dsh_cli`` and calls it. The one stub is
``asyncio.create_subprocess_exec`` — the boundary where the process stops
being ours — and the scripted stdout is the report line the resumable surface
writes. Nothing here asserts on a value the test computed.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from minds.harness import dsh_cli


class _FakeStdout:
    """The spawned process's stdout, line by line."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for line in self._lines:
                yield line.encode()
        return gen()


class _FakeStderr:
    def __init__(self, text: str) -> None:
        self._text = text

    async def read(self) -> bytes:
        return self._text.encode()


class _FakeProc:
    def __init__(self, lines: list[str], stderr: str = "", returncode: int = 0) -> None:
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode
        self.pid = 4242

    async def wait(self) -> int:
        return self.returncode


class _Spawn:
    """Records the spawn and answers it with a scripted process."""

    def __init__(self, lines: list[str], stderr: str = "", returncode: int = 0,
                 on_spawn: Any = None) -> None:
        self.lines = lines
        self.stderr = stderr
        self.returncode = returncode
        self.on_spawn = on_spawn
        self.calls: list[dict] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> _FakeProc:
        self.calls.append({"argv": list(argv), **kwargs})
        if self.on_spawn is not None:
            self.on_spawn()
        return _FakeProc(self.lines, self.stderr, self.returncode)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1]["argv"]

    @property
    def env(self) -> dict[str, str]:
        return self.calls[-1]["env"]


class _FakeRequest:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


class _FakeSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


def _report(**overrides: Any) -> str:
    report = {
        "sessionId": "conv-1",
        "mode": "create",
        "outcome": "completed",
        "text": "the answer",
        "traffic": {"emitted": 0, "answered": 0, "succeeded": 0, "failed": 0,
                    "unanswered": 0, "failuresByCode": {}, "callsByTool": {}},
    }
    report.update(overrides)
    return json.dumps(report) + "\n"


@pytest.fixture()
def dsh(monkeypatch, tmp_path: Path):
    """The adapter with its own DSH_HOME and an empty session table."""
    monkeypatch.setattr(dsh_cli, "DSH_HOME", tmp_path)
    monkeypatch.setattr(dsh_cli, "PROJECT_DIR", Path("/work/app"))
    monkeypatch.setattr(dsh_cli, "SESSIONS", {})
    return dsh_cli


def _persist(module, session_id: str) -> Path:
    """Create the on-disk session directory dsh would write for this id."""
    directory = (
        module.DSH_HOME / "sessions"
        / module._project_key(str(module.PROJECT_DIR))
        / module._encode_segment(session_id)
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _session(module, sid: str = "conv-1", model: str = "qwen35-131k") -> dict:
    module.SESSIONS[sid] = {
        "system_prompt": "SOUL AND MEMORY",
        "model": model,
        "proc": None,
        "client_ref": "",
        "owner_type": "",
        "owner_ref": "",
    }
    return module.SESSIONS[sid]


async def _drain(module, sid: str, content: str = "do the thing") -> list[dict]:
    return [event async for event in module._run_dsh_turn(sid, content, None)]


def _result(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "result")


def _assistant_text(events: list[dict]) -> str:
    frame = next(e for e in events if e["type"] == "assistant")
    return frame["message"]["content"][0]["text"]


async def test_a_turn_with_no_model_is_refused(dsh) -> None:
    response = await dsh.create_session(_FakeRequest({"session_id": "conv-1"}))
    assert response.status_code == 400
    assert dsh.SESSIONS == {}


async def test_a_turn_with_no_conversation_id_is_refused(dsh) -> None:
    response = await dsh.create_session(_FakeRequest({"model": "qwen35-131k"}))
    assert response.status_code == 400
    assert dsh.SESSIONS == {}


async def test_the_conversations_first_turn_creates_under_the_gateways_id(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, "conv-1")
    assert "--session-id" in spawn.argv
    assert spawn.argv[spawn.argv.index("--session-id") + 1] == "conv-1"
    assert "--resume" not in spawn.argv


async def test_the_second_turn_on_that_id_continues_it(dsh, monkeypatch) -> None:
    _session(dsh)
    # A real dsh turn persists the session; the fake does the same so the
    # second spawn sees what the first one left behind.
    spawn = _Spawn([_report()], on_spawn=lambda: _persist(dsh, "conv-1"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, "conv-1")
    await _drain(dsh, "conv-1", "and now the next thing")
    assert "--resume" in spawn.argv
    assert spawn.argv[spawn.argv.index("--resume") + 1] == "conv-1"
    assert "--session-id" not in spawn.argv


async def test_a_session_dsh_already_holds_is_resumed_by_a_process_that_never_saw_it(
    dsh, monkeypatch
) -> None:
    # The mind-restart case: SESSIONS is fresh, the transcript is not.
    _persist(dsh, "conv-1")
    _session(dsh)
    spawn = _Spawn([_report(mode="resume")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, "conv-1")
    assert "--resume" in spawn.argv


async def test_the_turn_is_spawned_in_its_own_process_group(dsh, monkeypatch) -> None:
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, "conv-1")
    assert spawn.calls[-1]["start_new_session"] is True


async def test_killing_a_session_signals_the_process_group_not_the_pid(
    dsh, monkeypatch
) -> None:
    proc = _FakeProc([])
    proc.returncode = None
    _session(dsh)["proc"] = proc
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    monkeypatch.setattr(proc, "wait", _completed_wait(proc))
    await dsh.kill_session("conv-1")
    assert signalled == [(proc.pid + 1000, 9)]


def _completed_wait(proc: _FakeProc) -> Any:
    async def wait() -> int:
        proc.returncode = -9
        return -9
    return wait


async def test_the_assistant_text_the_turn_produced_reaches_the_operator(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(text="banana, obviously")]))
    assert _assistant_text(await _drain(dsh, "conv-1")) == "banana, obviously"


async def test_a_turn_that_produced_no_text_reports_what_the_model_did(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    traffic = {"emitted": 3, "answered": 2, "succeeded": 1, "failed": 1,
               "unanswered": 1, "failuresByCode": {"EPARSE": 1}, "callsByTool": {"read": 3}}
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(text="", outcome="max-tokens", traffic=traffic)]))
    text = _assistant_text(await _drain(dsh, "conv-1"))
    assert "max-tokens" in text
    assert "3 tool call" in text
    assert "read=3" in text


async def test_a_failed_turn_carries_dshs_own_error_code(dsh, monkeypatch) -> None:
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([_report(
        outcome="error", text="",
        error={"code": "EPROVIDER", "message": "upstream said no"},
    )]))
    result = _result(await _drain(dsh, "conv-1"))
    assert result["is_error"] is True
    assert result["error_code"] == "EPROVIDER"
    assert result["error"] == "upstream said no"


async def test_a_turn_that_completed_having_produced_nothing_is_not_a_failure(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(text="", outcome="completed")]))
    result = _result(await _drain(dsh, "conv-1"))
    assert result["is_error"] is False
    assert "error_code" not in result


async def test_a_process_that_wrote_no_report_is_reported_as_failed(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn(["booting\n"], stderr="Segmentation fault", returncode=139))
    events = await _drain(dsh, "conv-1")
    result = _result(events)
    assert result["is_error"] is True
    assert result["stop_reason"] == "no-report"
    assert "Segmentation fault" in _assistant_text(events)


async def test_the_stop_reason_is_recorded_verbatim(dsh, monkeypatch) -> None:
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(outcome="max-tokens")]))
    result = _result(await _drain(dsh, "conv-1"))
    assert result["stop_reason"] == "max-tokens"
    assert result["is_error"] is True


async def test_the_tool_traffic_reaches_the_result_told_apart_by_outcome(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    traffic = {"emitted": 3, "answered": 2, "succeeded": 1, "failed": 1,
               "unanswered": 1, "failuresByCode": {"EPARSE": 1}, "callsByTool": {"bash": 3}}
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(traffic=traffic)]))
    assert _result(await _drain(dsh, "conv-1"))["traffic"] == traffic


async def test_the_model_and_provider_come_from_the_minds_own_configuration(
    dsh, monkeypatch
) -> None:
    _session(dsh, model="gpt-oss:20b-32k")
    monkeypatch.setitem(dsh.RUNTIME_ENV, "OLLAMA_BASE_URL", "http://proxy:8899/v1/")
    monkeypatch.setitem(dsh.RUNTIME, "dsh_provider_route", "hive-proxy")
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, "conv-1")
    assert spawn.env["DSH_MODEL"] == "gpt-oss:20b-32k"
    assert spawn.env["DSH_PROVIDER"] == "hive-proxy"
    assert spawn.env["DSH_PROXY_BASE_URL"] == "http://proxy:8899/v1"


async def test_a_terminal_release_is_refused_as_unsupported(dsh) -> None:
    _session(dsh)
    response = await dsh.release_session("conv-1", surface="terminal")
    assert response.status_code == 501
    # The conversation is untouched: a refusal is not a release.
    assert "conv-1" in dsh.SESSIONS


async def test_a_terminal_attach_closes_on_its_own_code(dsh) -> None:
    socket = _FakeSocket()
    await dsh.attach_pty(socket)
    assert socket.accepted is True
    assert socket.closed is not None
    assert socket.closed[0] == 4417
