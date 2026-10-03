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


def _persist(module, encoded_id: str) -> Path:
    """Create the on-disk session directory dsh writes for an encoded id.

    Spelled out as literals rather than built by calling the encoder: the
    encoder is what the resume decision depends on, and a fixture that follows
    its mutations cannot tell a correct encoding from a collapsed one. The
    project segment is what dsh's own ``projectKey`` emits for ``/work/app``.
    """
    directory = module.DSH_HOME / "sessions" / "--work-app--" / encoded_id
    directory.mkdir(parents=True, exist_ok=True)
    # dsh's own probe is the log, and so is the adapter's.
    (directory / "session.jsonl.zstd").write_bytes(b"")
    return directory


def _session(module, sid: str = "row-1", model: str = "qwen35-131k",
             conversation_id: str = "conv-1") -> dict:
    module.SESSIONS[sid] = {
        "system_prompt": "SOUL AND MEMORY",
        "conversation_id": conversation_id,
        "model": model,
        "proc": None,
        "client_ref": "",
        "owner_type": "",
        "owner_ref": "",
    }
    return module.SESSIONS[sid]


async def _drain(module, sid: str = "row-1", content: str = "do the thing") -> list[dict]:
    return [event async for event in module._run_dsh_turn(sid, content, None)]


def _result(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "result")


def _assistant_text(events: list[dict]) -> str:
    frame = next(e for e in events if e["type"] == "assistant")
    return frame["message"]["content"][0]["text"]


def test_a_session_id_is_encoded_the_way_dshs_own_backend_encodes_it(dsh) -> None:
    """The expected values are dsh's own, taken from running ``encodeSegment``
    and ``projectKey`` in ``session-persistence-jsonl``. They are what decides
    which directory a resume looks in, so a divergence here means every
    conversation is created fresh forever and ``--resume`` never fires."""
    assert dsh._encode_segment("abc-123") == "abc-123"
    assert dsh._encode_segment("a~b/c") == "a~007Eb~002Fc"
    assert dsh._encode_segment("Ünïcode id") == "~00DCn~00EFcode~0020id"
    assert dsh._encode_segment("..") == "~002E~002E"
    # Code units, not code points: JavaScript's charCodeAt walks UTF-16, so an
    # astral character is two escapes. Verified against dsh's own encodeSegment.
    assert dsh._encode_segment("a\U0001F600b") == "a~D83D~DE00b"
    assert dsh._encode_segment(".") == "~002E"
    assert dsh._project_key("/usr/src/app") == "--usr-src-app--"
    assert dsh._project_key("/home/daniel//x") == "--home-daniel-x--"
    assert dsh._project_key("C:\\work\\p") == "--C-work-p--"


async def test_an_id_needing_escaping_is_found_under_its_escaped_directory(
    dsh, monkeypatch
) -> None:
    _persist(dsh, "conv~007E1")
    _session(dsh, conversation_id="conv~1")
    spawn = _Spawn([_report(sessionId="conv~1", mode="resume")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert "--resume" in spawn.argv


async def test_a_turn_with_no_model_is_refused(dsh) -> None:
    response = await dsh.create_session(
        _FakeRequest({"session_id": "row-1", "resume_sid": "conv-1"}))
    assert response.status_code == 400
    assert dsh.SESSIONS == {}


async def test_a_turn_with_no_conversation_id_is_refused(dsh) -> None:
    """`resume_sid` is the conversation id comms minted; the session row's own
    id is a different thing, and a harness run under it would resume the
    context a rotation exists to drop."""
    response = await dsh.create_session(
        _FakeRequest({"session_id": "row-1", "model": "qwen35-131k"}))
    assert response.status_code == 400
    assert dsh.SESSIONS == {}


async def test_the_turn_runs_in_the_conversation_the_gateway_minted(dsh, monkeypatch) -> None:
    """Not in the session row's id. A rotation keeps the row and replaces the
    conversation, so a harness pinned to the row would never see the reset."""
    await dsh.create_session(_FakeRequest({
        "session_id": "row-1", "resume_sid": "conv-xyz", "model": "qwen35-131k",
        "system_prompt_blocks": "SOUL",
    }))
    spawn = _Spawn([_report(sessionId="conv-xyz")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.argv[spawn.argv.index("--session-id") + 1] == "conv-xyz"


async def test_the_conversations_first_turn_creates_under_the_gateways_id(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert "--session-id" in spawn.argv
    assert spawn.argv[spawn.argv.index("--session-id") + 1] == "conv-1"
    assert "--resume" not in spawn.argv


async def test_the_second_turn_on_that_id_continues_it(dsh, monkeypatch) -> None:
    _session(dsh)
    # A real dsh turn persists the session; the fake does the same so the
    # second spawn sees what the first one left behind.
    spawn = _Spawn([_report()], on_spawn=lambda: _persist(dsh, "conv-1"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    await _drain(dsh, "row-1", "and now the next thing")
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
    await _drain(dsh)
    assert "--resume" in spawn.argv


async def test_the_turn_is_spawned_in_its_own_process_group(dsh, monkeypatch) -> None:
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
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
    await dsh.kill_session("row-1")
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
    assert _assistant_text(await _drain(dsh)) == "banana, obviously"


async def test_a_turn_that_produced_no_text_reports_what_the_model_did(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    traffic = {"emitted": 3, "answered": 2, "succeeded": 1, "failed": 1,
               "unanswered": 1, "failuresByCode": {"EPARSE": 1}, "callsByTool": {"read": 3}}
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(text="", outcome="max-tokens", traffic=traffic)]))
    text = _assistant_text(await _drain(dsh))
    assert "max-tokens" in text
    assert "3 tool call" in text
    assert "read=3" in text


async def test_a_failed_turn_carries_dshs_own_error_code(dsh, monkeypatch) -> None:
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([_report(
        outcome="error", text="",
        error={"code": "EPROVIDER", "message": "upstream said no"},
    )]))
    result = _result(await _drain(dsh))
    assert result["is_error"] is True
    assert result["error_code"] == "EPROVIDER"
    assert result["error"] == "upstream said no"


async def test_a_turn_that_completed_having_produced_nothing_is_not_a_failure(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(text="", outcome="completed")]))
    result = _result(await _drain(dsh))
    assert result["is_error"] is False
    assert "error_code" not in result


async def test_a_process_that_wrote_no_report_is_reported_as_failed(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn(["booting\n"], stderr="Segmentation fault", returncode=139))
    events = await _drain(dsh)
    result = _result(events)
    assert result["is_error"] is True
    assert result["stop_reason"] == "no-report"
    assert "Segmentation fault" in _assistant_text(events)


async def test_the_stop_reason_is_recorded_verbatim(dsh, monkeypatch) -> None:
    """Including a reason this harness has not grown yet. A pass-through is the
    only implementation that survives: anything keyed off a known set turns an
    unfamiliar reason into "unknown" and loses the measurement."""
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(outcome="max-tokens")]))
    assert _result(await _drain(dsh))["stop_reason"] == "max-tokens"

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(outcome="halted-by-the-moon")]))
    result = _result(await _drain(dsh))
    assert result["stop_reason"] == "halted-by-the-moon"
    assert result["is_error"] is True


async def test_the_tool_traffic_reaches_the_result_told_apart_by_outcome(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    traffic = {"emitted": 3, "answered": 2, "succeeded": 1, "failed": 1,
               "unanswered": 1, "failuresByCode": {"EPARSE": 1}, "callsByTool": {"bash": 3}}
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(traffic=traffic)]))
    assert _result(await _drain(dsh))["traffic"] == traffic


async def test_the_model_and_endpoint_come_from_the_minds_own_configuration(
    dsh, monkeypatch
) -> None:
    _session(dsh, model="gpt-oss:20b-32k")
    monkeypatch.setitem(dsh.RUNTIME_ENV, "OLLAMA_BASE_URL", "http://proxy:8899/v1/")
    monkeypatch.setitem(dsh.RUNTIME, "context_window", 32768)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env["DSH_MODEL"] == "gpt-oss:20b-32k"
    assert spawn.env["DSH_PROXY_BASE_URL"] == "http://proxy:8899/v1"
    assert spawn.env["DSH_MODEL_CONTEXT_WINDOW"] == "32768"
    # The route is the profile's own, because a YAML mapping key cannot be
    # computed and naming a route the profile does not declare fails the boot.
    assert spawn.env["DSH_PROVIDER"] == dsh.PROFILE_PROVIDER_ROUTE


async def test_the_proxy_credential_is_translated_to_the_name_the_route_resolves(
    dsh, monkeypatch
) -> None:
    """Every mind's env block spells its proxy key differently, the profile
    resolves one name, and the proxy answers 401 without a key — so an
    untranslated credential is every turn of this mind failing at its first
    model request."""
    _session(dsh)
    monkeypatch.delitem(dsh.RUNTIME_ENV, "OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(dsh.RUNTIME_ENV, "ANTHROPIC_AUTH_TOKEN", "sk-the-minds-own-key")
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env[dsh.PROXY_KEY_ENV] == "sk-the-minds-own-key"


async def test_a_launcher_that_cannot_start_is_reported_as_a_failure(
    dsh, monkeypatch
) -> None:
    """Not as a turn that produced nothing: a stream ending with no frames at
    all reaches the gateway as a quiet model, which is the wrong machine to go
    looking at when the harness is simply not installed."""
    _session(dsh)

    async def missing(*argv: str, **kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "dsh")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    events = await _drain(dsh)
    result = _result(events)
    assert result["is_error"] is True
    assert result["error_code"] == "HARNESS_NOT_SPAWNED"
    assert "could not be started" in _assistant_text(events)


async def test_an_empty_session_directory_is_not_a_resumable_conversation(
    dsh, monkeypatch
) -> None:
    """dsh creates the directory before it writes the first log, and refuses a
    resume it cannot load. Treating the bare directory as resumable would make
    a turn killed in that window refuse forever, with nothing to clean up."""
    (dsh.DSH_HOME / "sessions" / "--work-app--" / "conv-1").mkdir(parents=True)
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert "--session-id" in spawn.argv


async def test_the_turns_process_group_is_signalled_on_every_turn(
    dsh, monkeypatch
) -> None:
    """A tool's background child outlives the launcher, and a child that
    outlives its group reparents to PID 1 — which in the mind's container is
    this process."""
    _session(dsh)
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([_report()]))
    await _drain(dsh)
    assert signalled == [(5242, 9)]


async def test_the_composed_prompt_rides_in_on_the_conversations_first_turn(
    dsh, monkeypatch
) -> None:
    """And on no other. The system prompt is comms' composition — soul, memory
    and standing rules — and a mind whose first turn goes out without it is a
    mind with no identity, which no assertion about tool traffic would catch."""
    _session(dsh)
    captured: list[str] = []
    spawn = _Spawn([_report()])

    def read_task_file() -> None:
        path = spawn.argv[spawn.argv.index("--task-file") + 1]
        captured.append(Path(path).read_text(encoding="utf-8"))

    spawn.on_spawn = read_task_file
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, "row-1", "do the thing")
    assert "SOUL AND MEMORY" in captured[0]
    assert "do the thing" in captured[0]

    _persist(dsh, "conv-1")
    await _drain(dsh, "row-1", "and the next thing")
    assert "SOUL AND MEMORY" not in captured[1]
    assert captured[1] == "and the next thing"


async def test_the_task_never_travels_in_argv(dsh, monkeypatch) -> None:
    """A composed prompt plus a turn runs past MAX_ARG_STRLEN, which caps one
    argv entry at 128 KiB however much room the whole command line has."""
    _session(dsh)["system_prompt"] = "S" * 200_000
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, "row-1", "T" * 200_000)
    assert max(len(arg) for arg in spawn.argv) < 4096


async def test_every_spawn_names_the_profile_that_mounts_the_runner(
    dsh, monkeypatch
) -> None:
    """A profile is the only thing that composes a dsh process, so an unnamed
    one is a mind with no runner rather than a mind with different options."""
    _session(dsh)
    monkeypatch.setattr(dsh_cli, "DSH_PROFILE", "hive")
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert "--profile" in spawn.argv
    assert spawn.argv[spawn.argv.index("--profile") + 1] == "hive"


async def test_the_report_is_told_apart_from_the_processs_other_stdout(
    dsh, monkeypatch
) -> None:
    """Node warnings and plugin chatter share the stream with the one report
    line. Reading the wrong line would report a turn that did not happen."""
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([
        "booting the hive profile\n",
        '{"level":"warn","msg":"experimental type stripping"}\n',
        _report(outcome="max-tokens", text="the real answer"),
        "flushed 3 events\n",
    ]))
    result = _result(await _drain(dsh))
    assert result["stop_reason"] == "max-tokens"


async def test_the_rotation_hooks_own_metadata_reaches_the_spawn(
    dsh, monkeypatch
) -> None:
    """The Stop hook reads these off the process env to attribute a rotation
    summary to the right row. Unset, rotation silently never arms."""
    state = _session(dsh)
    state.update({"client_ref": "tg-123", "owner_type": "telegram", "owner_ref": "chat-9"})
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env["CLIENT_REF"] == "tg-123"
    assert spawn.env["OWNER_TYPE"] == "telegram"
    assert spawn.env["OWNER_REF"] == "chat-9"


async def test_a_second_turn_on_a_busy_conversation_is_refused(dsh) -> None:
    """Two dsh processes resuming one session store is how a transcript ends up
    holding two interleaved turns and neither one's history intact."""
    _session(dsh)["in_flight"] = True
    response = await dsh.send_message("row-1", _FakeRequest({"content": "hello"}))
    assert response.status_code == 409


async def test_the_session_the_turn_reports_is_the_session_reported_upward(
    dsh, monkeypatch
) -> None:
    """dsh runs in the id it was handed, so the report's own session id is the
    one the gateway hears back — not a local copy that could drift from it."""
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(sessionId="conv-1-as-dsh-saw-it")]))
    assert _result(await _drain(dsh))["session_id"] == "conv-1-as-dsh-saw-it"


async def test_a_terminal_release_is_refused_as_unsupported(dsh) -> None:
    _session(dsh)
    response = await dsh.release_session("row-1", surface="terminal")
    assert response.status_code == 501
    # The conversation is untouched: a refusal is not a release.
    assert "row-1" in dsh.SESSIONS


def test_a_terminal_attach_closes_on_its_own_code(dsh) -> None:
    """Driven through the real app, not by calling the handler: a handler whose
    socket parameter is not typed as a WebSocket is never handed one — FastAPI
    reads it as a required query parameter and closes before accepting, which
    presents to the gateway as HTTP 403, the same answer a mind with no pty
    route gives. The close code is the whole point of the route."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    with TestClient(dsh.app) as client:
        with client.websocket_connect("/sessions/row-1/attach-pty") as socket:
            with pytest.raises(WebSocketDisconnect) as refused:
                socket.receive_text()
    assert refused.value.code == 4417
