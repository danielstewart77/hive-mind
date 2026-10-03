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
    """The spawned process's stdout, line by line.

    ``readline`` is what the adapter actually uses, so it can tick a heartbeat
    between lines; ``line_delay`` is how long a line keeps it waiting.
    """

    def __init__(self, lines: list[str], line_delay: float = 0.0) -> None:
        self._lines = list(lines)
        self._delay = line_delay

    async def readline(self) -> bytes:
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._lines:
            return b""
        return self._lines.pop(0).encode()

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            while True:
                line = await self.readline()
                if not line:
                    return
                yield line
        return gen()


class _FakeStderr:
    def __init__(self, text: str) -> None:
        self._text = text

    async def read(self) -> bytes:
        return self._text.encode()


class _FakeProc:
    def __init__(self, lines: list[str], stderr: str = "", returncode: int = 0,
                 line_delay: float = 0.0) -> None:
        self.stdout = _FakeStdout(lines, line_delay)
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode
        self.pid = 4242

    async def wait(self) -> int:
        return self.returncode


class _Spawn:
    """Records the spawn and answers it with a scripted process."""

    def __init__(self, lines: list[str], stderr: str = "", returncode: int = 0,
                 on_spawn: Any = None, line_delay: float = 0.0) -> None:
        self.lines = lines
        self.stderr = stderr
        self.returncode = returncode
        self.on_spawn = on_spawn
        self.line_delay = line_delay
        self.calls: list[dict] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> _FakeProc:
        self.calls.append({"argv": list(argv), **kwargs})
        if self.on_spawn is not None:
            self.on_spawn()
        return _FakeProc(self.lines, self.stderr, self.returncode, self.line_delay)

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
    monkeypatch.setattr(dsh_cli, "SPAWN_DIR", Path("/work/app"))
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


async def test_a_mind_under_test_asks_the_harness_to_stop_at_a_refusal(
    dsh, monkeypatch
) -> None:
    """The flag is per mind, not per turn: a chat mind must not end a
    conversation because one tool call came back refused."""
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setitem(dsh.RUNTIME, "stop_on_dialect_gap", True)
    await _drain(dsh)
    assert "--stop-on-dialect-gap" in spawn.argv


async def test_an_ordinary_mind_drives_through_a_refused_call(dsh, monkeypatch) -> None:
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.delitem(dsh.RUNTIME, "stop_on_dialect_gap", raising=False)
    await _drain(dsh)
    assert "--stop-on-dialect-gap" not in spawn.argv


async def test_the_turn_runs_in_the_work_area_the_deployment_named(
    dsh, monkeypatch, tmp_path: Path
) -> None:
    """A model writing a relative path lands in the spawn's working directory.
    The default is the mind's own tree, which is where an exam's output must
    never go, so a deployment that mounts a work area names it and the spawn
    runs there."""
    work = tmp_path / "exam-work-area"
    work.mkdir()
    monkeypatch.setattr(dsh_cli, "SPAWN_DIR", work)
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.calls[-1]["cwd"] == str(work)


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


async def test_the_spawn_runs_under_a_mode_with_no_approval_wall_to_answer(
    dsh, monkeypatch
) -> None:
    """dsh pins a fresh session to workspace-write and `ask`, and this harness
    has nobody to ask — so a shell call comes back SANDBOX_UNAVAILABLE and a
    write outside the spawn cwd comes back FS_SANDBOX_DENIED, both reported as
    things the model cannot do rather than walls the deployment put up."""
    _session(dsh)
    monkeypatch.delitem(dsh.RUNTIME, "permission_mode", raising=False)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env["DSH_PERMISSION_MODE"] == dsh.DEFAULT_PERMISSION_MODE


async def test_a_mind_that_wants_confinement_names_its_own_permission_mode(
    dsh, monkeypatch
) -> None:
    """The container is the boundary by default, but a sandboxed mind on a
    machine nobody wants it operating can say so in its own runtime.yaml."""
    _session(dsh)
    monkeypatch.setitem(dsh.RUNTIME, "permission_mode", "read-only")
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env["DSH_PERMISSION_MODE"] == "read-only"


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


async def test_respawning_a_session_mid_turn_does_not_erase_the_turn_guard(
    dsh
) -> None:
    """comms respawns on its own restart, mid-turn, because its process table
    is empty while this process and its running dsh turn are not. A fresh state
    dict would put a second process on the same session log, and two writers
    numbering events from the same prefix make that log unloadable — after
    which dsh refuses both resume and create and the conversation is dead."""
    _session(dsh)["in_flight"] = True
    await dsh.create_session(_FakeRequest({
        "session_id": "row-1", "resume_sid": "conv-1", "model": "qwen35-131k",
    }))
    assert dsh.SESSIONS["row-1"]["in_flight"] is True
    response = await dsh.send_message("row-1", _FakeRequest({"content": "hello"}))
    assert response.status_code == 409


async def test_a_turn_that_never_ends_is_stopped_and_reported(
    dsh, monkeypatch
) -> None:
    """A model request that hangs rather than fails would otherwise hold the
    conversation open until the gateway's socket read expired and reported a
    stalled model as a mind that cannot be reached."""
    _session(dsh)
    monkeypatch.setattr(dsh_cli, "TURN_TIMEOUT_SECONDS", 0.05)

    class _Hangs(_FakeProc):
        def __init__(self) -> None:
            super().__init__([])

        def __post_init__(self) -> None:  # pragma: no cover - not used
            pass

    class _HangingStdout:
        async def readline(self) -> bytes:
            await asyncio.sleep(30)
            return b""

    async def spawn(*argv: str, **kwargs: Any) -> _FakeProc:
        proc = _FakeProc([])
        proc.stdout = _HangingStdout()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    events = await _drain(dsh)
    result = _result(events)
    assert result["stop_reason"] == "timeout"
    assert result["error_code"] == "TURN_TIMEOUT"
    assert "stopped" in _assistant_text(events)


async def test_output_the_adapter_cannot_read_is_reported_not_swallowed(
    dsh, monkeypatch
) -> None:
    """A single stdout line past the stream limit raises after the SSE headers
    are already out. Unhandled, the response ends with no frames at all and the
    gateway reports a turn that produced nothing — silence, for the user."""
    _session(dsh)

    class _TooLong:
        async def readline(self) -> bytes:
            raise ValueError("Separator is not found, and chunk exceed the limit")

    async def spawn(*argv: str, **kwargs: Any) -> _FakeProc:
        proc = _FakeProc([])
        proc.stdout = _TooLong()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    events = await _drain(dsh)
    assert _result(events)["is_error"] is True
    assert "exceed the limit" in _assistant_text(events)


async def test_a_turn_stopped_on_purpose_is_not_reported_as_a_crash(
    dsh, monkeypatch
) -> None:
    """A kill or a cross-surface release ends the process mid-turn. The
    harness-exited-without-reporting sentence would read as a crash to the one
    person who knows they asked for it."""
    state = _session(dsh)
    state["killed"] = True
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([], stderr="", returncode=-9))
    result = _result(await _drain(dsh))
    assert result["stop_reason"] == "stopped"
    assert result["is_error"] is False


async def test_an_attached_image_is_reported_as_not_sent(dsh, monkeypatch) -> None:
    """A model answering the text as if nothing was attached looks like a model
    that ignored the picture."""
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([_report()]))
    events = [e async for e in dsh._run_dsh_turn("row-1", "what is this?",
                                                 [{"data": "x"}, {"data": "y"}])]
    texts = [e["message"]["content"][0]["text"] for e in events if e["type"] == "assistant"]
    assert any("2 attached image(s) were not sent" in t for t in texts)


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


async def test_a_chat_minds_dispatch_is_one_turn_and_names_no_rounds(
    dsh, monkeypatch
) -> None:
    """A person talking to a mind expects their message answered once. Driving
    every chat turn as a goal would have the model carry on talking to itself
    after it had already replied."""
    _session(dsh)
    monkeypatch.delitem(dsh.RUNTIME, "goal_rounds", raising=False)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert "--goal-rounds" not in spawn.calls[0]["argv"]


async def test_a_build_mind_drives_its_dispatch_for_the_rounds_it_declared(
    dsh, monkeypatch
) -> None:
    """A mind whose job is a long build names a round cap in its own
    runtime.yaml, and then its first natural pause ends a round rather than the
    job — which is the only thing that reliably stops a small model quitting at
    the two-minute mark."""
    _session(dsh)
    monkeypatch.setitem(dsh.RUNTIME, "goal_rounds", 40)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    argv = spawn.calls[0]["argv"]
    assert argv[argv.index("--goal-rounds") + 1] == "40"


async def test_a_round_cap_that_cannot_be_read_is_one_turn_not_a_crash(
    dsh, monkeypatch
) -> None:
    """A hand-edited runtime.yaml holding nonsense costs the long-running
    behaviour, not the mind: every turn still answers."""
    _session(dsh)
    monkeypatch.setitem(dsh.RUNTIME, "goal_rounds", "lots")
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert "--goal-rounds" not in spawn.calls[0]["argv"]


async def test_the_turn_count_and_goal_phase_reach_the_gateway(dsh, monkeypatch) -> None:
    """A build that stopped at round two of forty and one that burned all forty
    are different failures, and the tool tally alone cannot tell them apart."""
    _session(dsh)
    spawn = _Spawn([_report(turns=17, goalPhase="active")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = _result(await _drain(dsh))
    assert (result["turns"], result["goal_phase"]) == (17, "active")


async def test_each_goal_round_crosses_the_socket_as_an_observer_frame(
    dsh, monkeypatch
) -> None:
    """comms caps the response socket on time since the last byte, and a
    goal-driven dispatch is one response lasting an hour — so a round that
    writes nothing is a mind that looks like it stopped answering. Observer-only
    because the chat surface wants the answer, not a running count."""
    _session(dsh)
    monkeypatch.setitem(dsh.RUNTIME, "goal_rounds", 40)
    spawn = _Spawn([
        json.dumps({"progress": {"round": 1, "turns": 2, "toolCalls": 30}}) + "\n",
        json.dumps({"progress": {"round": 2, "turns": 3, "toolCalls": 51}}) + "\n",
        _report(turns=3),
    ])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    events = await _drain(dsh)
    rounds = [e for e in events if e.get("type") == "goal_progress"]
    assert [e["progress"]["round"] for e in rounds] == [1, 2]
    assert all(e["_observer_only"] for e in rounds)


async def test_a_progress_line_is_not_mistaken_for_the_turn_report(dsh, monkeypatch) -> None:
    """The report is identified by `sessionId` and is scanned for from the end,
    so a progress line carrying one would end the turn at the first round."""
    _session(dsh)
    monkeypatch.setitem(dsh.RUNTIME, "goal_rounds", 40)
    spawn = _Spawn([
        _report(turns=1, text="the real answer"),
        json.dumps({"sessionId": "conv-1", "progress": {"round": 9}}) + "\n",
    ])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert _assistant_text(await _drain(dsh)) == "the real answer"


async def test_the_goal_objective_is_the_message_not_the_composed_prompt(
    dsh, monkeypatch, tmp_path
) -> None:
    """The round driver quotes the objective into every round, so a goal armed
    with a conversation's opening task would spend the context window on forty
    copies of the soul and system prompt."""
    _session(dsh)
    monkeypatch.setitem(dsh.RUNTIME, "goal_rounds", 40)
    seen: dict[str, str] = {}
    spawn = _Spawn([_report()])

    original = spawn.__call__

    async def capture(*argv: str, **kwargs: Any):
        objective = argv[argv.index("--goal-objective-file") + 1]
        seen["objective"] = Path(objective).read_text(encoding="utf-8")
        seen["task"] = Path(argv[argv.index("--task-file") + 1]).read_text(encoding="utf-8")
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    await _drain(dsh, content="build the app")
    assert seen["objective"] == "build the app"
    assert len(seen["task"]) > len(seen["objective"])


async def test_the_objective_file_is_cleaned_up_with_the_task_file(
    dsh, monkeypatch
) -> None:
    """One process owns both files; a turn that leaves them behind fills /tmp
    with composed prompts."""
    _session(dsh)
    monkeypatch.setitem(dsh.RUNTIME, "goal_rounds", 40)
    paths: list[str] = []
    spawn = _Spawn([_report()])
    original = spawn.__call__

    async def capture(*argv: str, **kwargs: Any):
        paths.append(argv[argv.index("--goal-objective-file") + 1])
        paths.append(argv[argv.index("--task-file") + 1])
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    await _drain(dsh)
    assert paths and not [p for p in paths if Path(p).exists()]


async def test_a_quiet_turn_still_puts_a_byte_on_the_socket(dsh, monkeypatch) -> None:
    """comms caps the mind response socket on time since the last byte. This
    harness writes nothing until a turn ends, so a turn that thinks for longer
    than that cap is read as a mind that stopped answering — and comms aborting
    the response kills this process group while the work is still going."""
    _session(dsh)
    monkeypatch.setattr(dsh, "HEARTBEAT_SECONDS", 0.01)
    spawn = _Spawn([_report()], line_delay=0.05)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    events = await _drain(dsh)
    beats = [e for e in events if e.get("type") == "turn_heartbeat"]
    assert beats and all(e["_observer_only"] for e in beats)
    # And the turn still answers: a heartbeat is not a substitute for the report.
    assert _assistant_text(events) == "the answer"


async def test_a_line_is_not_lost_to_a_heartbeat_tick(dsh, monkeypatch) -> None:
    """The read task is awaited across ticks rather than cancelled on each one;
    cancelling a readline mid-line drops the line, and the dropped line would be
    the turn report."""
    _session(dsh)
    monkeypatch.setattr(dsh, "HEARTBEAT_SECONDS", 0.01)
    spawn = _Spawn([
        json.dumps({"progress": {"round": 1, "turns": 2, "toolCalls": 9}}) + "\n",
        _report(turns=2, text="finished"),
    ], line_delay=0.03)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    events = await _drain(dsh)
    assert [e["progress"]["round"] for e in events if e.get("type") == "goal_progress"] == [1]
    assert _result(events)["turns"] == 2
