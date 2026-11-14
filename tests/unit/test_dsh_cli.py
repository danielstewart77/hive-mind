"""The dsh harness adapter: one resident process per conversation.

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


def _set_live(monkeypatch, dsh, key: str, value) -> None:
    """Set one per-turn setting where the adapter reads it.

    `goal_rounds`, `stop_on_failed_call` and the turn timeout are re-read from
    `runtime.yaml` on every turn, so the console's settings panel takes effect
    on the next turn rather than on the next container start. A test that set
    them on the boot-time copy would be setting them where nothing looks.
    """
    live = dict(dsh.RUNTIME)
    if value is None:
        live.pop(key, None)
    else:
        live[key] = value
    monkeypatch.setattr(dsh, "live_runtime", lambda: live)


class _FakeStdout:
    """The spawned process's stdout, fed line by line as the process writes.

    A queue rather than a fixed list, because a resident process writes when it
    is spoken to: its readiness line lands at spawn and a turn's lines land
    when the turn is written to its stdin. ``line_delay`` is how long a line
    keeps the reader waiting.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        #: How long each line keeps the reader waiting. Set after the
        #: readiness line is fed, so a slow turn is not also a slow boot: the
        #: readiness wait is its own boundary and not what these tests drive.
        self.delay = 0.0

    @property
    def unread(self) -> int:
        """Lines written but not yet read — how far behind the reader is."""
        return self._queue.qsize()

    def feed(self, line: str) -> None:
        self._queue.put_nowait(line.encode())

    def eof(self) -> None:
        self._queue.put_nowait(b"")

    async def readline(self) -> bytes:
        if self.delay:
            await asyncio.sleep(self.delay)
        return await self._queue.get()


class _FakeStderr:
    def __init__(self, text: str) -> None:
        self._text = text

    async def read(self) -> bytes:
        return self._text.encode()


#: The line a resident process writes before it reads its first turn.
_READY = json.dumps({"ready": {"sessionId": "conv-1", "mode": "create",
                               "model": "qwen35-131k"}}) + "\n"


class _ReadyThenHangs(_FakeStdout):
    """A process that announces its conversation and then never writes again.

    The readiness line is its own boundary with its own wait; what these tests
    drive is the turn deadline, which only starts once there is a process to
    write a turn to.
    """

    def __init__(self) -> None:
        super().__init__()
        self.feed(_READY)

    async def readline(self) -> bytes:
        if self.unread:
            return await super().readline()
        await asyncio.sleep(30)
        return b""


class _FakeStdin:
    """The resident process's stdin: one turn request per line."""

    def __init__(self, proc: "_FakeProc") -> None:
        self._proc = proc

    def write(self, data: bytes) -> None:
        self._proc.take(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


def _has_report(lines: list[str]) -> bool:
    """Whether a script answers a turn at all, as the adapter reads a report."""
    for line in lines:
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "sessionId" in parsed and "progress" not in parsed:
            return True
    return False


class _FakeProc:
    """A scripted dsh process, resident or one-shot.

    Resident: the readiness line lands at spawn, and every turn written to
    stdin is answered with the script — with this turn's id stamped onto the
    report and onto any completed message, which is what the real runner does
    and what the adapter matches on. A script carrying no report is a process
    that died without answering, so it ends its stdout rather than hanging the
    turn that is waiting.

    One-shot (a goal dispatch): the whole script is written at spawn and the
    stream ends, which is the shape that process actually has.
    """

    def __init__(self, lines: list[str], stderr: str = "", returncode: int = 0,
                 line_delay: float = 0.0, resident: bool = True,
                 ready: bool = True, hold_report: bool = False,
                 answer_id: str | None = None) -> None:
        self.stdout = _FakeStdout()
        self.stderr = _FakeStderr(stderr)
        self.stdin = _FakeStdin(self) if resident else None
        self.returncode: int | None = None if resident else returncode
        self._exit = returncode
        self.pid = 4242
        self.script = list(lines)
        self.requests: list[dict] = []
        #: Answer a turn with everything but its report, until `finish` is
        #: called. A frame that reaches the surface while the report is still
        #: being withheld cannot have been buffered until the turn ended,
        #: because nothing that could end it has been written yet.
        self.hold_report = hold_report
        self._held: list[str] = []
        #: Answer every turn under this id instead of the one that asked —
        #: what a report left over from an abandoned turn looks like arriving
        #: while a later one is waiting.
        self.answer_id = answer_id
        if resident:
            if ready:
                self.stdout.feed(_READY)
        else:
            for line in lines:
                self.stdout.feed(line)
            self.stdout.eof()
        self.stdout.delay = line_delay

    def take(self, data: bytes) -> None:
        """Answer one turn request the way the resident runner answers it."""
        request = json.loads(data.decode())
        self.requests.append(request)
        turn_id = self.answer_id or request.get("id")
        for line in self.script:
            stamped = _stamp(line, turn_id)
            if self.hold_report and _has_report([line]):
                self._held.append(stamped)
                continue
            self.stdout.feed(stamped)
        if self.hold_report:
            return
        if not _has_report(self.script):
            # Nothing answered this turn: the process is gone.
            self.returncode = self._exit
            self.stdout.eof()

    def speak(self, text: str) -> None:
        """Write a message nobody asked for, as a settled background child
        makes this process do: dsh opens a turn on the idle owner and the
        runner marks what it says unsolicited, because no turn is in flight."""
        self.stdout.feed(json.dumps({"message": text, "unsolicited": True}) + "\n")

    def finish(self) -> None:
        """Write the report this process was holding back."""
        for line in self._held:
            self.stdout.feed(line)
        self._held = []

    async def wait(self) -> int:
        self.returncode = self._exit
        return self._exit


def _stamp(line: str, turn_id: str | None) -> str:
    """Put this turn's id on the lines the runner puts it on."""
    text = line.strip()
    if turn_id is None or not text.startswith("{"):
        return line
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return line
    if not isinstance(parsed, dict):
        return line
    if "sessionId" in parsed or isinstance(parsed.get("message"), str):
        parsed["id"] = turn_id
        return json.dumps(parsed) + "\n"
    return line


class _Spawn:
    """Records the spawn and answers it with a scripted process."""

    def __init__(self, lines: list[str], stderr: str = "", returncode: int = 0,
                 on_spawn: Any = None, line_delay: float = 0.0,
                 ready: bool = True, answer_id: str | None = None) -> None:
        self.lines = lines
        self.stderr = stderr
        self.returncode = returncode
        self.on_spawn = on_spawn
        self.line_delay = line_delay
        self.ready = ready
        self.answer_id = answer_id
        self.calls: list[dict] = []
        self.procs: list[_FakeProc] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> _FakeProc:
        self.calls.append({"argv": list(argv), **kwargs})
        if self.on_spawn is not None:
            self.on_spawn()
        proc = _FakeProc(self.lines, self.stderr, self.returncode, self.line_delay,
                         resident="--resident" in argv, ready=self.ready,
                         answer_id=self.answer_id)
        self.procs.append(proc)
        return proc

    @property
    def argv(self) -> list[str]:
        return self.calls[-1]["argv"]

    @property
    def env(self) -> dict[str, str]:
        return self.calls[-1]["env"]

    @property
    def turns(self) -> list[dict]:
        """Every turn request written to a resident process, in order."""
        return [request for proc in self.procs for request in proc.requests]


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
    # A readiness wait short enough that a process which never announces itself
    # is a failed test rather than a two-minute one. The real value is the
    # boundary, not the behaviour under test.
    monkeypatch.setattr(dsh_cli, "RESIDENT_READY_SECONDS", 2.0)
    monkeypatch.setattr(dsh_cli, "PROACTIVE_BUFFER", [])
    # Each model is sized by the window the proxy lists for it, asked per turn.
    proxy(monkeypatch, {"qwen35-131k": 131_072, "gpt-oss:20b-32k": 32_768})
    return dsh_cli


class _Listing:
    """The inference proxy's `/v1/models`, at the HTTP boundary."""

    def __init__(self, windows: dict[str, int | None]) -> None:
        self.windows = windows
        self.urls: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        listing = self

        class _Resp:
            status = 200

            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def json(self) -> dict:
                return {"data": [{"id": name, "context_window": window}
                                 for name, window in listing.windows.items()]}

        class _Session:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            def get(self, url: str, **kw: Any) -> Any:
                listing.urls.append(url)
                return _Resp()

        return _Session()


def proxy(monkeypatch, windows: dict[str, int | None]) -> _Listing:
    """Answer the model listing with these windows, for this mind's key."""
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.test")
    monkeypatch.setenv("MIND_PROXY_KEY", "hmp-test")
    fake = _Listing(windows)
    monkeypatch.setattr(dsh_cli.models_api.aiohttp, "ClientSession", fake)
    return fake


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
        "surface": "",
        "surface_id": "",
    }
    return module.SESSIONS[sid]


async def _drain(module, sid: str = "row-1", content: str = "do the thing",
                 goal_rounds: object = None) -> list[dict]:
    return [event async for event in module._run_dsh_turn(sid, content, None, goal_rounds)]


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
    response = await dsh.start_session({"session_id": "row-1", "resume_sid": "conv-1"})
    assert response.status_code == 400
    assert dsh.SESSIONS == {}


async def test_a_turn_with_no_conversation_id_is_refused(dsh) -> None:
    """`resume_sid` is the conversation id comms minted; the session row's own
    id is a different thing, and a harness run under it would resume the
    context a rotation exists to drop."""
    response = await dsh.start_session({"session_id": "row-1", "model": "qwen35-131k"})
    assert response.status_code == 400
    assert dsh.SESSIONS == {}


async def test_the_turn_runs_in_the_conversation_the_gateway_minted(dsh, monkeypatch) -> None:
    """Not in the session row's id. A rotation keeps the row and replaces the
    conversation, so a harness pinned to the row would never see the reset."""
    await dsh.start_session({
        "session_id": "row-1", "resume_sid": "conv-xyz", "model": "qwen35-131k",
        "system_prompt_blocks": "SOUL",
    })
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
    _set_live(monkeypatch, dsh, "stop_on_failed_call", True)
    await _drain(dsh)
    assert "--stop-on-failed-call" in spawn.argv


async def test_an_ordinary_mind_drives_through_a_refused_call(dsh, monkeypatch) -> None:
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    _set_live(monkeypatch, dsh, "stop_on_failed_call", None)
    await _drain(dsh)
    assert "--stop-on-failed-call" not in spawn.argv


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


async def test_the_second_turn_is_written_to_the_process_the_first_one_started(
    dsh, monkeypatch
) -> None:
    """The residency itself. A second process would be a second conversation
    on one session store — colliding event sequence numbers, a log dsh can
    then load for neither resume nor create — and a background child the first
    turn dispatched would have died with it."""
    _session(dsh)
    spawn = _Spawn([_report()], on_spawn=lambda: _persist(dsh, "conv-1"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    await _drain(dsh, "row-1", "and now the next thing")
    assert len(spawn.calls) == 1
    assert [request["turn"] for request in spawn.turns][1] == "and now the next thing"


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

    # A second conversation, because the first one's process is resident and
    # answers from the script it was spawned with.
    _session(dsh, "row-2", conversation_id="conv-2")
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(outcome="halted-by-the-moon")]))
    result = _result(await _drain(dsh, "row-2"))
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
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env["DSH_MODEL"] == "gpt-oss:20b-32k"
    assert spawn.env["DSH_PROXY_BASE_URL"] == "http://proxy:8899/v1"


async def test_a_standalone_turn_is_sized_by_the_window_the_proxy_lists_for_its_model(
    dsh, monkeypatch
) -> None:
    """Nothing composed this conversation's window for it, so the turn asks the
    proxy — and a serving ceiling in the mind's own file does not stand in for
    it, because one number for every model is room some of them do not have."""
    _session(dsh, model="gpt-oss:20b-32k")
    monkeypatch.setitem(dsh.RUNTIME, "context_window", 1_000_000)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env["DSH_MODEL_CONTEXT_WINDOW"] == "32768"


async def test_the_window_composed_for_the_conversation_wins_over_the_listing(
    dsh, monkeypatch
) -> None:
    """The mind server sized this conversation for the model it is on; the
    turn runs under that, not under a second lookup or the mind's own file."""
    session = _session(dsh, model="qwen35-131k")
    session["conversation_env"] = {"HIVE_MODEL_CONTEXT_WINDOW": "65536"}
    monkeypatch.setitem(dsh.RUNTIME, "context_window", 1_000_000)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env["DSH_MODEL_CONTEXT_WINDOW"] == "65536"


async def test_a_model_the_proxy_lists_no_window_for_is_refused_and_the_operator_told(
    dsh, monkeypatch
) -> None:
    """A model without a window cannot be run: guessing one is how a
    conversation overflows without ever rotating. The refusal fires before the
    first yield, and an unhandled one would end the response with zero frames —
    which the gateway reports as a turn that produced nothing."""
    _session(dsh, model="mystery-model")
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    events = await _drain(dsh)
    assert "mystery-model" in _assistant_text(events)
    assert _result(events)["error_code"] == "MIND_MISCONFIGURED"
    assert spawn.calls == []


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
    for name in dsh._PROXY_KEY_SOURCES:
        monkeypatch.delenv(name, raising=False)
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


async def test_a_goal_dispatchs_process_group_is_signalled_when_its_run_ends(
    dsh, monkeypatch
) -> None:
    """A goal run owns its process, and a tool's background child outlives the
    launcher: a child that outlives its group reparents to PID 1, which in the
    mind's container is this process. A chat turn's group is not signalled
    here, because the process it shares is the conversation's."""
    _session(dsh)
    _set_live(monkeypatch, dsh, "goal_rounds", 4)
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([_report()]))
    await _drain(dsh, goal_rounds=4)
    assert signalled == [(5242, 9)]


async def test_the_composed_prompt_rides_in_on_the_conversations_first_turn(
    dsh, monkeypatch
) -> None:
    """And on no other. The system prompt is comms' composition — soul, memory
    and standing rules — and a mind whose first turn goes out without it is a
    mind with no identity, which no assertion about tool traffic would catch."""
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, "row-1", "do the thing")
    assert "SOUL AND MEMORY" in spawn.turns[0]["turn"]
    assert "do the thing" in spawn.turns[0]["turn"]

    await _drain(dsh, "row-1", "and the next thing")
    assert "SOUL AND MEMORY" not in spawn.turns[1]["turn"]
    assert spawn.turns[1]["turn"] == "and the next thing"


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
    state.update({"client_ref": "tg-123", "surface": "telegram", "surface_id": "chat-9"})
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env["CLIENT_REF"] == "tg-123"
    assert spawn.env["SURFACE"] == "telegram"
    assert spawn.env["SURFACE_ID"] == "chat-9"


async def test_a_second_turn_on_a_busy_conversation_is_refused(dsh) -> None:
    """Two dsh processes resuming one session store is how a transcript ends up
    holding two interleaved turns and neither one's history intact."""
    _session(dsh)["in_flight"] = True
    response = await dsh.send("row-1", {"content": "hello"})
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


async def test_a_terminal_release_ends_the_tmux_owned_process(dsh, monkeypatch) -> None:
    _session(dsh)
    released: list[str] = []
    monkeypatch.setattr(dsh, "teardown_pty", lambda sid: released.append(sid) or True)
    response = await dsh.release_session("row-1", surface="terminal")
    assert response["released"] is True
    assert released == ["row-1"]
    # Releasing a surface preserves the durable conversation and session row.
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
    await dsh.start_session({
        "session_id": "row-1", "resume_sid": "conv-1", "model": "qwen35-131k",
    })
    assert dsh.SESSIONS["row-1"]["in_flight"] is True
    response = await dsh.send("row-1", {"content": "hello"})
    assert response.status_code == 409


async def test_a_turn_that_never_ends_is_stopped_and_reported(
    dsh, monkeypatch
) -> None:
    """A model request that hangs rather than fails would otherwise hold the
    conversation open until the gateway's socket read expired and reported a
    stalled model as a mind that cannot be reached."""
    _session(dsh)
    monkeypatch.setattr(
        dsh_cli, "live_runtime", lambda: {**dsh_cli.RUNTIME, "turn_timeout_seconds": 0.05}
    )

    class _Hangs(_FakeProc):
        def __init__(self) -> None:
            super().__init__([])

        def __post_init__(self) -> None:  # pragma: no cover - not used
            pass

    async def spawn(*argv: str, **kwargs: Any) -> _FakeProc:
        proc = _FakeProc([])
        proc.stdout = _ReadyThenHangs()
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
    person who knows they asked for it.

    The flag is set *during* the turn, which is the only way it is ever set:
    one left over from a previous turn is cleared when this one starts, so a
    stale flag cannot make an unrelated failure look deliberate."""
    state = _session(dsh)
    spawn = _Spawn([], stderr="", returncode=-9)

    async def kill_mid_turn(*argv, **kwargs):
        proc = await spawn(*argv, **kwargs)
        state["killed"] = True
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", kill_mid_turn)
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


class _FakeTerminals:
    def __init__(self, alive: bool = False) -> None:
        self.is_alive = alive
        self.starts: list[dict[str, Any]] = []
        self.attaches: list[dict[str, Any]] = []
        self.respawns: list[dict[str, Any]] = []

    def alive(self, session_id: str) -> bool:
        return self.is_alive

    def start(self, session_id: str, argv: list[str], **kwargs: Any) -> None:
        self.starts.append({"session_id": session_id, "argv": argv, **kwargs})
        self.is_alive = True

    def attach(self, session_id: str, **kwargs: Any) -> tuple[Any, int]:
        self.attaches.append({"session_id": session_id, **kwargs})
        return type("Proc", (), {"pid": 9191})(), 17

    def respawn(self, session_id: str, argv: list[str], **kwargs: Any) -> None:
        self.respawns.append({"session_id": session_id, "argv": argv, **kwargs})


def test_a_cold_terminal_opens_the_gateway_conversation_with_its_context(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    terminals = _FakeTerminals()
    monkeypatch.setattr(dsh, "TERMINALS", terminals)
    proc, master_fd = dsh._spawn_pty(
        session_id="row-1", model="qwen35-131k", conversation_id="conv-1",
        cols=100, rows=30,
    )
    argv = terminals.starts[0]["argv"]
    assert argv[:3] == [dsh.DSH_BIN, "--profile", dsh.DSH_PROFILE]
    assert argv[argv.index("--session-id") + 1] == "conv-1"
    assert "--interactive" in argv
    context = Path(argv[argv.index("--context-file") + 1])
    assert context.read_text(encoding="utf-8") == "SOUL AND MEMORY"
    assert (proc.pid, master_fd) == (9191, 17)


def test_the_pane_is_told_which_mind_it_speaks_for(dsh, monkeypatch) -> None:
    """The dsh terminal prints its own prompt and banner from MIND_NAME."""
    _session(dsh)
    terminals = _FakeTerminals()
    monkeypatch.setattr(dsh, "TERMINALS", terminals)
    dsh._spawn_pty(
        session_id="row-1", model="qwen35-131k", conversation_id="conv-1",
        cols=100, rows=30,
    )
    assert terminals.starts[0]["env_overrides"]["MIND_NAME"] == dsh.NAME


def test_a_persisted_terminal_resumes_without_reapplying_opening_context(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    _persist(dsh, "conv-1")
    terminals = _FakeTerminals()
    monkeypatch.setattr(dsh, "TERMINALS", terminals)
    dsh._spawn_pty(
        session_id="row-1", model="qwen35-131k", conversation_id="conv-1",
        cols=80, rows=24,
    )
    argv = terminals.starts[0]["argv"]
    assert argv[argv.index("--resume") + 1] == "conv-1"
    assert "--context-file" not in argv


def test_a_terminal_cannot_join_while_a_chat_turn_writes_the_transcript(
    dsh, monkeypatch
) -> None:
    _session(dsh)["in_flight"] = True
    monkeypatch.setattr(dsh, "TERMINALS", _FakeTerminals())
    with pytest.raises(dsh.PtyUnavailable, match="chat turn"):
        dsh._spawn_pty(
            session_id="row-1", model="qwen35-131k", conversation_id="conv-1",
            cols=80, rows=24,
        )


def test_rotation_respawns_the_same_pane_on_the_new_conversation(
    dsh, monkeypatch
) -> None:
    terminals = _FakeTerminals(alive=True)
    monkeypatch.setattr(dsh, "TERMINALS", terminals)
    assert dsh._rotate_pty(
        session_id="row-1", new_conversation_id="conv-2", model="qwen35-131k",
        system_prompt="carry forward",
    ) is True
    argv = terminals.respawns[0]["argv"]
    assert argv[argv.index("--session-id") + 1] == "conv-2"
    context = Path(argv[argv.index("--context-file") + 1])
    assert context.read_text(encoding="utf-8") == "carry forward"


def test_a_staged_rotation_asks_dsh_to_answer_its_opening_context(
    dsh, monkeypatch
) -> None:
    """dsh queues an opening context unless told to answer it.

    An adapter that reads only `system_prompt`, or that omits the flag, hands
    the pane the user's question with no reply coming.
    """
    terminals = _FakeTerminals(alive=True)
    monkeypatch.setattr(dsh, "TERMINALS", terminals)
    assert dsh._rotate_pty(
        session_id="row-1", new_conversation_id="conv-3", model="qwen35-131k",
        system_prompt="the summary",
        user_prompt="the summary\n\nand what I typed",
    ) is True
    argv = terminals.respawns[0]["argv"]
    assert "--context-as-turn" in argv
    context = Path(argv[argv.index("--context-file") + 1])
    assert context.read_text(encoding="utf-8") == "the summary\n\nand what I typed"


def test_a_whitespace_seed_opens_the_pane_unseeded_rather_than_killing_it(
    dsh, monkeypatch
) -> None:
    """dsh refuses a context it cannot make a turn out of, and a refusal here
    exits the process — a dead pane, after rotate-pty has already answered
    `rotated: true` and the gateway has written the successor's id."""
    terminals = _FakeTerminals(alive=True)
    monkeypatch.setattr(dsh, "TERMINALS", terminals)
    assert dsh._rotate_pty(
        session_id="row-1", new_conversation_id="conv-4", model="qwen35-131k",
        user_prompt="   \n\t ",
    ) is True
    argv = terminals.respawns[0]["argv"]
    assert "--context-file" not in argv
    assert "--context-as-turn" not in argv


async def test_a_chat_minds_dispatch_is_one_turn_and_names_no_rounds(
    dsh, monkeypatch
) -> None:
    """A person talking to a mind expects their message answered once. Driving
    every chat turn as a goal would have the model carry on talking to itself
    after it had already replied."""
    _session(dsh)
    _set_live(monkeypatch, dsh, "goal_rounds", None)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert "--goal-rounds" not in spawn.calls[0]["argv"]


async def test_a_dispatch_that_asks_for_no_goal_is_one_turn_on_a_mind_that_names_rounds(
    dsh, monkeypatch
) -> None:
    """Rounds are not a property of the mind: the same mind is asked a question
    in one conversation and handed a build in another. A number in its file
    arms nothing by itself."""
    _session(dsh)
    _set_live(monkeypatch, dsh, "goal_rounds", 40)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert "--goal-rounds" not in spawn.calls[0]["argv"]


async def test_a_dispatch_that_asks_for_a_goal_is_driven_for_the_rounds_it_carries(
    dsh, monkeypatch
) -> None:
    """The build orchestrator's dispatch carries its round count, and then the
    model's first natural pause ends a round rather than the job."""
    _session(dsh)
    _set_live(monkeypatch, dsh, "goal_rounds", None)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    response = await dsh.send("row-1", {"content": "build the app", "goal_rounds": 40})
    [chunk async for chunk in response.body_iterator]
    argv = spawn.calls[0]["argv"]
    assert argv[argv.index("--goal-rounds") + 1] == "40"


async def test_the_minds_own_round_count_caps_what_a_dispatch_may_ask_for(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    _set_live(monkeypatch, dsh, "goal_rounds", 3)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, goal_rounds=40)
    argv = spawn.calls[0]["argv"]
    assert argv[argv.index("--goal-rounds") + 1] == "3"


async def test_the_rotation_check_is_told_it_is_answering_a_goal_round(
    dsh, monkeypatch
) -> None:
    """Only in a goal run does dsh act on the check's answer, so only there
    does the check decide before it detaches and say "rotate" on stdout."""
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, goal_rounds=40)
    await _drain(dsh)
    assert spawn.calls[0]["env"].get("HIVE_GOAL_RUN") == "1"
    assert "HIVE_GOAL_RUN" not in spawn.calls[1]["env"]


async def test_a_round_count_that_cannot_be_read_is_one_turn_not_a_crash(
    dsh, monkeypatch
) -> None:
    """A malformed dispatch costs the long-running behaviour, not the turn."""
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh, goal_rounds="lots")
    assert "--goal-rounds" not in spawn.calls[0]["argv"]


async def test_a_goal_run_ended_for_rotation_reaches_the_gateway_with_its_goal(
    dsh, monkeypatch
) -> None:
    """dsh never rotates itself. It ends the run and says what was left, and
    the gateway carries the objective and the rounds onto a fresh conversation
    — which it can only do if both arrive."""
    _session(dsh)
    goal = {"objective": "build the app", "roundsLeft": 2}
    spawn = _Spawn([_report(outcome="rotation_needed", goal=goal)])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = _result(await _drain(dsh, goal_rounds=5))
    assert result["stop_reason"] == "rotation_needed"
    assert result["goal"] == goal


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
    spawn = _Spawn([
        json.dumps({"progress": {"round": 1, "turns": 2, "toolCalls": 30}}) + "\n",
        json.dumps({"progress": {"round": 2, "turns": 3, "toolCalls": 51}}) + "\n",
        _report(turns=3),
    ])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    events = await _drain(dsh, goal_rounds=40)
    rounds = [e for e in events if e.get("type") == "goal_progress"]
    assert [e["progress"]["round"] for e in rounds] == [1, 2]
    assert all(e["_observer_only"] for e in rounds)


async def test_a_progress_line_is_not_mistaken_for_the_turn_report(dsh, monkeypatch) -> None:
    """The report is identified by `sessionId` and is scanned for from the end,
    so a progress line carrying one would end the turn at the first round."""
    _session(dsh)
    spawn = _Spawn([
        _report(turns=1, text="the real answer"),
        json.dumps({"sessionId": "conv-1", "progress": {"round": 9}}) + "\n",
    ])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert _assistant_text(await _drain(dsh, goal_rounds=40)) == "the real answer"


async def test_the_goal_objective_is_the_message_not_the_composed_prompt(
    dsh, monkeypatch, tmp_path
) -> None:
    """The round driver quotes the objective into every round, so a goal armed
    with a conversation's opening task would spend the context window on forty
    copies of the soul and system prompt."""
    _session(dsh)
    seen: dict[str, str] = {}
    spawn = _Spawn([_report()])

    original = spawn.__call__

    async def capture(*argv: str, **kwargs: Any):
        objective = argv[argv.index("--goal-objective-file") + 1]
        seen["objective"] = Path(objective).read_text(encoding="utf-8")
        seen["task"] = Path(argv[argv.index("--task-file") + 1]).read_text(encoding="utf-8")
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    await _drain(dsh, content="build the app", goal_rounds=40)
    assert seen["objective"] == "build the app"
    assert len(seen["task"]) > len(seen["objective"])


async def test_the_objective_file_is_cleaned_up_with_the_task_file(
    dsh, monkeypatch
) -> None:
    """One process owns both files; a turn that leaves them behind fills /tmp
    with composed prompts."""
    _session(dsh)
    paths: list[str] = []
    spawn = _Spawn([_report()])
    original = spawn.__call__

    async def capture(*argv: str, **kwargs: Any):
        paths.append(argv[argv.index("--goal-objective-file") + 1])
        paths.append(argv[argv.index("--task-file") + 1])
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    await _drain(dsh, goal_rounds=40)
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


class TestTurnTimeoutResolution:
    """Zero is a choice, not a missing value.

    `float(raw or 1800)` treated zero as absent and handed back thirty
    minutes, so a mind asking for no bound got a tighter one than the default
    it was trying to escape.
    """

    def test_zero_means_no_bound_at_all(self) -> None:
        assert dsh_cli.turn_timeout({"turn_timeout_seconds": 0}) is None

    def test_an_absent_value_takes_the_default(self) -> None:
        assert dsh_cli.turn_timeout({}) == dsh_cli.DEFAULT_TURN_TIMEOUT_SECONDS

    def test_a_declared_number_is_the_bound(self) -> None:
        assert dsh_cli.turn_timeout({"turn_timeout_seconds": 14400}) == 4 * 3600

    def test_an_unreadable_value_takes_the_default(self) -> None:
        """A mind with a typo in its file is bounded, not unbounded."""
        assert (
            dsh_cli.turn_timeout({"turn_timeout_seconds": "soon"})
            == dsh_cli.DEFAULT_TURN_TIMEOUT_SECONDS
        )


async def test_a_turn_the_console_unbounded_is_not_killed(dsh, monkeypatch) -> None:
    """The settings panel offers zero as no bound, so a turn past the default
    deadline has to complete rather than be stopped."""
    _session(dsh)
    monkeypatch.setattr(
        dsh_cli, "live_runtime", lambda: {**dsh_cli.RUNTIME, "turn_timeout_seconds": 0}
    )
    monkeypatch.setattr(dsh_cli, "DEFAULT_TURN_TIMEOUT_SECONDS", 0.05)

    report = {"sessionId": "32db455c", "text": "done", "outcome": "completed"}

    async def spawn(*argv: str, **kwargs: Any) -> _FakeProc:
        return _FakeProc([json.dumps(report) + "\n"], line_delay=0.2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    events = await _drain(dsh)
    assert _result(events)["stop_reason"] == "completed"
    assert "done" in _assistant_text(events)


class TestInterrupt:
    """`/stop` on a surface reaches this route, and it has to actually stop.

    A per-turn harness has no interrupt of its own, so the process is the only
    thing there is to stop. The route used to answer `ok` without touching it,
    which told the operator the work had been interrupted while a forty-round
    dispatch carried on behind the message.
    """

    async def test_it_kills_the_process_group_of_the_turn_in_flight(
        self, dsh, monkeypatch
    ) -> None:
        session = _session(dsh)
        killed: list[Any] = []

        async def reap(proc: Any) -> None:
            killed.append(proc)

        monkeypatch.setattr(dsh, "_reap_proc", reap)
        running = _FakeProc([])
        running.returncode = None
        session["proc"] = running

        body = await dsh.interrupt_session("row-1")
        assert body["message"] == "interrupted"
        assert killed == [running]
        assert session["killed"] is True
        assert session["proc"] is None

    async def test_a_session_with_nothing_running_says_so(self, dsh) -> None:
        """"Interrupted" over an idle session is a lie the operator acts on."""
        _session(dsh)
        body = await dsh.interrupt_session("row-1")
        assert body["message"] == "nothing_running"

    async def test_an_unknown_session_is_a_404(self, dsh) -> None:
        response = await dsh.interrupt_session("no-such-row")
        assert response.status_code == 404

    async def test_a_fresh_turn_is_not_stopped_by_the_previous_interrupt(
        self, dsh, monkeypatch
    ) -> None:
        """The flag is the last turn's verdict. Left standing, the next turn
        whose output is unreadable reports itself as deliberately stopped."""
        session = _session(dsh)
        session["killed"] = True
        report = {"sessionId": "conv-1", "text": "back at it", "outcome": "completed"}

        async def spawn(*argv: str, **kwargs: Any) -> _FakeProc:
            return _FakeProc([json.dumps(report) + "\n"])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        events = await _drain(dsh)
        assert _result(events)["stop_reason"] == "completed"
        assert session["killed"] is False


async def test_the_profile_is_read_per_turn_so_an_edit_does_not_wait_on_a_restart(
    dsh, monkeypatch
) -> None:
    """The settings panel offers it, and a profile read once at import is a
    box whose value does nothing until somebody bounces the container —
    where a profile that is not installed fails far from the edit."""
    _session(dsh)
    _set_live(monkeypatch, dsh, "dsh_profile", "exam")
    argv: list[tuple] = []

    async def spawn(*args: str, **kwargs: Any) -> _FakeProc:
        argv.append(args)
        return _FakeProc([_report()])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    command = list(argv[0])
    assert command[command.index("--profile") + 1] == "exam"


async def _until(turn: Any, events: list[dict], frame_type: str) -> dict:
    """Consume the turn up to its first frame of this type, and return it.

    Every frame consumed on the way is kept, so the caller can go on draining
    the rest into the same list and read the whole turn afterwards.
    """
    async for event in turn:
        events.append(event)
        if event.get("type") == frame_type:
            return event
    raise AssertionError(f"the turn ended without a {frame_type} frame")


async def _answer_with(proc: _FakeProc) -> _FakeProc:
    """Spawn one prepared process, so a test can read how far its stdout got."""
    return proc


def _delta_line(kind: str, text: str) -> str:
    """One assistant delta the way the resumable runner writes it."""
    return json.dumps({"delta": {"kind": kind, "text": text}}) + "\n"


def _message_line(text: str) -> str:
    """One completed assistant message the way the resumable runner writes it."""
    return json.dumps({"message": text}) + "\n"


def _assistant_texts(events: list[dict]) -> list[str]:
    """Every whole assistant message handed over, in the order it was."""
    return [block["text"]
            for event in events if event.get("type") == "assistant"
            for block in event["message"]["content"]
            if block.get("type") == "text"]


def _streamed(events: list[dict], inner_type: str) -> list[str]:
    """The streamed pieces of one kind, in the order a surface receives them."""
    field = "thinking" if inner_type == "thinking_delta" else "text"
    return [
        event["event"]["delta"][field]
        for event in events
        if event.get("type") == "stream_event"
        and event["event"].get("type") == "content_block_delta"
        and event["event"]["delta"].get("type") == inner_type
    ]


async def test_prose_reaches_the_surface_as_the_model_writes_it(dsh, monkeypatch) -> None:
    """The frame is handed over while the process is still running.

    Collecting the frames and asserting their order proves nothing here: an
    adapter that buffered every delta and flushed them in order just before the
    report would produce the identical list, and the operator would be back to
    staring at a frozen chat for the whole turn. So this reads how much of the
    process's stdout had been consumed at the moment each frame arrived.
    """
    _session(dsh)
    lines = [
        _delta_line("text", "half "),
        _delta_line("text", "an answer"),
        _report(text="half an answer"),
    ]
    proc = _FakeProc(lines, hold_report=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        lambda *a, **k: _answer_with(proc))
    turn = dsh._run_dsh_turn("row-1", "do the thing", None)
    events: list[dict] = []
    # Nothing that could end the turn has been written yet, so an adapter that
    # held its deltas until the report would never get here.
    first = await asyncio.wait_for(_until(turn, events, "stream_event"), timeout=5.0)
    assert first["event"]["delta"]["text"] == "half "
    proc.finish()
    events.extend([event async for event in turn])

    assert _streamed(events, "text_delta") == ["half ", "an answer"]
    # Reaching the surface is the point: an observer-only frame is published to
    # the session event stream and never handed to the bot.
    assert all(not event.get("_observer_only")
               for event in events if event.get("type") == "stream_event")


async def test_the_models_reasoning_is_streamed_apart_from_its_answer(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([
        _delta_line("reasoning", "weighing it"),
        _delta_line("text", "the answer"),
        _report(text="the answer"),
    ]))
    events = await _drain(dsh)
    assert _streamed(events, "thinking_delta") == ["weighing it"]
    assert _streamed(events, "text_delta") == ["the answer"]


async def test_a_streamed_answer_still_reaches_the_turn_ledger(
    dsh, monkeypatch
) -> None:
    """The buffered frame is what comms accumulates into `session_turns`.

    Dropping it because the surface had already shown every word left the
    session holding the question and no reply, which is what `/history`, the
    rotation carry-forward and the late-turn merge all read. Showing it twice is
    the chat surfaces' problem and they already solve it.
    """
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([
        _delta_line("text", "the answer"),
        _report(text="the answer"),
    ]))
    events = await _drain(dsh)
    assert _assistant_text(events) == "the answer"


async def test_a_turn_that_only_reasoned_still_says_what_it_did(
    dsh, monkeypatch
) -> None:
    _session(dsh)
    traffic = {"emitted": 2, "answered": 2, "succeeded": 2, "failed": 0,
               "unanswered": 0, "failuresByCode": {}, "callsByTool": {"read": 2}}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([
        _delta_line("reasoning", "thinking about it"),
        _report(text="", outcome="max-tokens", traffic=traffic),
    ]))
    text = _assistant_text(await _drain(dsh))
    assert "max-tokens" in text
    assert "2 tool call" in text


async def test_a_kind_this_adapter_does_not_know_is_not_relayed_as_the_answer(
    dsh, monkeypatch
) -> None:
    """Anything but prose and reasoning is dropped rather than shown as prose."""
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([
        json.dumps({"delta": {"kind": "tool", "text": '{"path": "a'}}) + "\n",
        _delta_line("text", "the answer"),
        _report(text="the answer"),
    ]))
    events = await _drain(dsh)
    assert _streamed(events, "text_delta") == ["the answer"]


# ---------------------------------------------------------------------------
# A turn dsh fired no Stop for still gets the rotation check
# ---------------------------------------------------------------------------

class _CheckStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.mark.parametrize("ending", ["timeout", "killed", "no-report", "unparseable"])
async def test_a_turn_dsh_fired_no_stop_for_runs_the_minds_rotation_check(
    dsh, monkeypatch, tmp_path, ending,
) -> None:
    """dsh fires Stop only from a turn it ended itself. One that timed out, was
    killed, crashed or wrote nothing readable fired none, and what it read
    before it stopped is in the conversation all the same — so the adapter
    runs the check the mind registered for Stop, as a failed turn."""
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "bash /minds/x/hooks/auto_remember.sh"},
        {"type": "command", "command": "bash /minds/x/hooks/rotation_check.sh"},
    ]}]}}))
    monkeypatch.setenv("DSH_HOOKS_CONFIG", str(hooks))
    state = _session(dsh)
    if ending == "timeout":
        monkeypatch.setattr(
            dsh_cli, "live_runtime", lambda: {**dsh_cli.RUNTIME, "turn_timeout_seconds": 0.05})
    checks: list[tuple[list[str], _CheckStdin]] = []

    async def spawn(*argv: str, **kwargs: Any) -> _FakeProc:
        if argv[0] != dsh_cli.DSH_BIN:
            proc = _FakeProc([])
            proc.stdin = _CheckStdin()
            checks.append((list(argv), proc.stdin))
            return proc
        if ending == "timeout":
            proc = _FakeProc([])
            proc.stdout = _ReadyThenHangs()
            return proc
        if ending == "killed":
            state["killed"] = True
            return _FakeProc([], returncode=-9)
        if ending == "no-report":
            return _FakeProc([], stderr="Segmentation fault", returncode=139)
        return _FakeProc(["{not json\n"], returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)

    assert [argv for argv, _ in checks] == [["bash", "/minds/x/hooks/rotation_check.sh"]]
    payload = json.loads(checks[0][1].data)
    assert (payload["session_id"], payload["stop_reason"]) == ("conv-1", "error")


async def test_an_overflow_dsh_words_its_own_way_is_reported_under_the_one_code(
    dsh, monkeypatch
) -> None:
    """The gateway rotates on one code for every harness and never reads the
    text, so a provider's own wording of an overflow is coded here."""
    _session(dsh)
    spawn = _Spawn([_report(outcome="error", error={
        "code": "LLM_ERROR",
        "message": "This model's maximum context length is 131072 tokens."})])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert _result(await _drain(dsh))["error_code"] == "CONTEXT_WINDOW_EXCEEDED"


async def test_a_finished_message_is_delivered_while_the_turn_works_on(
    dsh, monkeypatch
) -> None:
    """Deltas keep the surface moving but carry no boundary.

    A mind that speaks, dispatches delegates and speaks again streams one run
    of prose and then goes quiet for as long as the delegates take, leaving
    the reader with a message that stops mid-thought. Daniel's words: "your
    text just cuts off and it waits until the other agents finish."
    """
    _session(dsh)
    lines = [
        _delta_line("text", "Dispatched three "),
        _delta_line("text", "jobs."),
        _message_line("Dispatched three jobs."),
        _delta_line("text", "Cypher is back: green."),
        _message_line("Cypher is back: green."),
        _report(text="Cypher is back: green."),
    ]
    proc = _FakeProc(lines, hold_report=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        lambda *a, **k: _answer_with(proc))
    turn = dsh._run_dsh_turn("row-1", "do the thing", None)
    events: list[dict] = []
    # The report is still being withheld, so the message that arrives here is
    # one the turn handed over while it was still working.
    first = await asyncio.wait_for(_until(turn, events, "assistant"), timeout=5.0)
    assert first["message"]["content"][0]["text"] == "Dispatched three jobs."
    proc.finish()
    events.extend([event async for event in turn])

    assert _assistant_texts(events)[0] == "Dispatched three jobs."
    # Reaching the surface is the point.
    assert all(not event.get("_observer_only")
               for event in events if event.get("type") == "assistant")


async def test_a_message_frame_is_not_made_from_a_fragment_or_a_report(
    dsh,
) -> None:
    """A surface sending one message per token would send a hundred."""
    assert dsh._message_frame(_delta_line("text", "half ")) is None
    assert dsh._message_frame(_report(text="done")) is None
    assert dsh._message_frame(
        json.dumps({"progress": {"round": 2}, "message": "x"})) is None
    assert dsh._message_frame(
        json.dumps({"sessionId": "conv-1", "message": "done"})) is None


async def test_node_chatter_never_becomes_a_message(dsh) -> None:
    assert dsh._message_frame("(node:7) ExperimentalWarning: blah") is None
    assert dsh._message_frame('{"message": ') is None
    assert dsh._message_frame(json.dumps({"message": ""})) is None
    assert dsh._message_frame(json.dumps({"message": 7})) is None
    assert dsh._message_frame(json.dumps(["message"])) is None


async def test_the_turn_ledger_still_gets_the_buffered_copy(
    dsh, monkeypatch
) -> None:
    """Unchanged by the message frames, and for the same reason as before.

    The buffered frame is what comms accumulates into `session_turns`, which
    `/history`, the rotation carry-forward and the late-turn merge all read.
    Showing it twice is the chat surfaces' problem and they already solve it.
    """
    _session(dsh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawn([
        _message_line("the answer"),
        _report(text="the answer"),
    ]))

    events = await _drain(dsh)

    assert _assistant_texts(events) == ["the answer", "the answer"]


# ---------------------------------------------------------------------------
# One process per conversation, and what it says when no turn asked
# ---------------------------------------------------------------------------

def _chat_session(module, sid: str = "row-1", conversation_id: str = "conv-1") -> dict:
    """A session on a surface that can take an unsolicited turn."""
    state = _session(module, sid, conversation_id=conversation_id)
    state["surface_label"] = "telegram"
    state["client_ref"] = "8776938611"
    return state


async def _settle(times: int = 40) -> None:
    """Let the reader task run. It is a task, not a coroutine the turn awaits,
    so what it does after the turn's last frame needs a tick to happen in."""
    for _ in range(times):
        await asyncio.sleep(0)


async def test_a_message_nobody_asked_for_is_handed_to_the_minds_surface(
    dsh, monkeypatch
) -> None:
    """The point of the whole change. A background child settles after the turn
    that dispatched it has closed, dsh opens a turn on the idle mind, and what
    the mind says then has no request to answer — so it goes to the surface by
    the proactive route rather than into a stream nobody is reading."""
    _chat_session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)

    spawn.procs[0].speak("Cypher is back: the build is green.")
    await _settle()

    assert dsh.PROACTIVE_BUFFER == [
        {"chat_id": 8776938611, "text": "Cypher is back: the build is green."}]


async def test_an_unsolicited_message_is_not_mistaken_for_the_next_turns_answer(
    dsh, monkeypatch
) -> None:
    """It is delivered on its own. Folding it into the next turn would answer
    Daniel's next question with a report about something else."""
    _chat_session(dsh)
    spawn = _Spawn([_report(text="the answer")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    spawn.procs[0].speak("a delegate came back refused")
    await _settle()

    events = await _drain(dsh, "row-1", "and now the next thing")
    assert _assistant_texts(events) == ["the answer"]


async def test_a_surface_that_cannot_take_a_delivery_is_not_handed_one(
    dsh, monkeypatch
) -> None:
    """A Discord snowflake is nineteen digits and `int()` takes it without
    complaint, which is how one mind's unsolicited turn was posted at a
    Telegram chat id that did not exist, sixty times over three hours."""
    state = _chat_session(dsh)
    state["surface_label"] = "discord"
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    spawn.procs[0].speak("a delegate came back refused")
    await _settle()

    assert dsh.PROACTIVE_BUFFER == []


async def test_a_process_that_died_between_turns_is_replaced(dsh, monkeypatch) -> None:
    """The conversation is on disk, so the next turn resumes it in a new
    process. Writing to the dead one would be a turn with no reply at all."""
    _session(dsh)
    spawn = _Spawn([_report()], on_spawn=lambda: _persist(dsh, "conv-1"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    await spawn.procs[0].wait()

    events = await _drain(dsh, "row-1", "and now the next thing")
    assert len(spawn.calls) == 2
    assert "--resume" in spawn.argv
    assert _result(events)["is_error"] is False


async def test_a_conversation_picked_up_on_another_surface_gets_a_process_told_so(
    dsh, monkeypatch
) -> None:
    """The surface a turn answers on rides in the process environment, so a
    resident one spawned for Telegram would keep answering there after the
    conversation moved to the browser."""
    state = _chat_session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    state["surface_label"] = "terminal"
    await _drain(dsh, "row-1", "and now from the terminal")

    assert [call["env"].get("HIVE_SURFACE") for call in spawn.calls] == ["telegram", "terminal"]


async def test_a_goal_dispatch_does_not_share_a_process_with_the_conversation(
    dsh, monkeypatch
) -> None:
    """Two processes on one session store write colliding event sequence
    numbers, and dsh then refuses to load the log for either resume or create —
    which ends the conversation for good, not just the turn."""
    _session(dsh)
    _set_live(monkeypatch, dsh, "goal_rounds", 4)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert dsh.SESSIONS["row-1"].get("resident") is not None

    await _drain(dsh, goal_rounds=4)
    assert dsh.SESSIONS["row-1"].get("resident") is None
    assert "--task-file" in spawn.argv
    assert "--resident" not in spawn.argv


async def test_an_idle_conversation_is_not_interrupted_out_of_its_residency(
    dsh, monkeypatch
) -> None:
    """There is nothing to interrupt between turns, and killing the process
    would take the one thing that can hear a background child settle."""
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)

    answer = await dsh.interrupt_session("row-1")
    assert answer["message"] == "nothing_running"
    assert dsh.SESSIONS["row-1"].get("resident") is not None


async def test_closing_a_session_ends_the_process_holding_it(dsh, monkeypatch) -> None:
    """A conversation nobody is in must not leave a node process running."""
    _session(dsh)
    killed: list[int] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1000)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append(pgid))
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)

    await dsh.kill_session("row-1")
    assert killed == [5242]


class TestWhatOneLineFromAResidentProcessIs:
    """The routing. Which of these a line is decides whether it reaches the
    turn that is waiting, the turn that asked for it by id, or Daniel."""

    def test_a_report_is_the_turn_it_names(self, dsh) -> None:
        routed = dsh._resident_dispatch(_report(id="t7"))
        assert (routed["kind"], routed["id"]) == ("report", "t7")

    def test_a_report_is_never_read_as_a_message(self, dsh) -> None:
        """It carries the final text beside `sessionId`, and reading it as a
        message would deliver the answer twice."""
        assert dsh._resident_dispatch(_report(text="the answer"))["kind"] == "report"

    def test_a_message_with_no_turn_behind_it_is_unsolicited(self, dsh) -> None:
        routed = dsh._resident_dispatch(
            json.dumps({"message": "a child settled", "unsolicited": True}))
        assert (routed["kind"], routed["text"]) == ("unsolicited", "a child settled")

    def test_a_message_the_turn_asked_for_carries_that_turns_id(self, dsh) -> None:
        routed = dsh._resident_dispatch(json.dumps({"message": "half an answer", "id": "t7"}))
        assert (routed["kind"], routed["id"]) == ("frame", "t7")

    def test_the_readiness_line_belongs_to_the_spawn(self, dsh) -> None:
        assert dsh._resident_dispatch(_READY)["kind"] == "ready"

    def test_a_line_the_runner_could_not_read_is_a_refusal(self, dsh) -> None:
        routed = dsh._resident_dispatch(json.dumps({"refused": "not JSON"}))
        assert (routed["kind"], routed["detail"]) == ("refused", "not JSON")

    def test_nodes_own_chatter_is_not_a_frame(self, dsh) -> None:
        assert dsh._resident_dispatch("(node:17) ExperimentalWarning\n") is None


async def test_a_report_for_another_turn_does_not_answer_this_one(
    dsh, monkeypatch
) -> None:
    """A turn that timed out is abandoned, and its report can still arrive. Read
    as this turn's, it would hand Daniel the answer to the question before his
    and record it in the ledger under the wrong one — so the id the request
    carried is what the report is matched on."""
    _session(dsh)
    monkeypatch.setattr(
        dsh_cli, "live_runtime", lambda: {**dsh_cli.RUNTIME, "turn_timeout_seconds": 0.2})
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _Spawn([_report(text="the other answer")], answer_id="t-stale"))

    events = await _drain(dsh)
    assert _result(events)["stop_reason"] == "timeout"
    assert "the other answer" not in " ".join(_assistant_texts(events))


async def test_the_turn_carries_the_gateway_session_id_a_mind_needs_to_end_itself(
    dsh, monkeypatch
) -> None:
    """A mind's `/end-session` deletes the gateway's row for the conversation
    it is in, and this harness exports no conversation id of its own to look
    one up by — so without this, ending a session is impossible on a dsh mind
    and the skill reports that the session identifier is unavailable."""
    _session(dsh)
    spawn = _Spawn([_report()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await _drain(dsh)
    assert spawn.env["HIVE_SESSION_ID"] == "row-1"


def test_the_terminals_pane_carries_the_same_session_id(dsh) -> None:
    """A pane is a conversation too, and the operator typing end-session in
    the browser terminal is the one most likely to."""
    env = dsh._pane_env("qwen35-131k", "", "", "", None, "row-1")
    assert env["HIVE_SESSION_ID"] == "row-1"
