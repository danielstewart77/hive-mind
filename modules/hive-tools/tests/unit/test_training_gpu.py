"""Unit tests for GPU arbitration around a fine-tune run.

Docker and nvidia-smi are subprocess calls and Ollama is HTTP, so the seams
are patched — but every test here is written to fail against an
implementation that merely *issues* the right calls. What is asserted is
the state the machine is left in: which containers are up, whether the
journal survives, and whether a run that ended is reported as having ended
the way it actually did.
"""

from __future__ import annotations

import json

import pytest

from tools import training_gpu as gpu


class FakeDocker:
    """A Docker daemon with container states you can move underneath it."""

    def __init__(self, states: dict[str, str], unreachable: bool = False):
        self.states = dict(states)
        self.unreachable = unreachable
        self.calls: list[list[str]] = []
        self.start_failures: set[str] = set()

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        if self.unreachable:
            raise gpu.DockerUnreachable("dial unix /var/run/docker.sock: permission denied")
        verb = args[0]
        if verb == "inspect":
            name = args[-1]
            if name not in self.states:
                return _Completed(1, "", f"Error: No such object: {name}")
            state = self.states[name]
            if "{{.State.ExitCode}}" in args[2]:
                status, _, code = state.partition(":")
                return _Completed(0, f"{status} {code or 0}", "")
            return _Completed(0, state.partition(":")[0], "")
        if verb == "stop":
            self.states[args[-1]] = "exited"
            return _Completed(0, "", "")
        if verb == "start":
            if args[-1] in self.start_failures:
                return _Completed(1, "", "start failed")
            self.states[args[-1]] = "running"
            return _Completed(0, "", "")
        if verb == "rm":
            self.states.pop(args[-1], None)
            return _Completed(0, "", "")
        return _Completed(0, "", "")


class _Completed:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def journal(tmp_path):
    return tmp_path / "gpu_arbitration.json"


@pytest.fixture()
def no_ollama(monkeypatch):
    """Ollama absent by default; tests that care patch it explicitly."""
    evicted: list[str] = []

    async def fake_evict(url=None):
        evicted.append("called")
        return ["qwen3-embedding:8b"]

    monkeypatch.setattr(gpu, "evict_models", fake_evict)
    return evicted


# ------------------------------------------------------------- requirement 1


async def test_preflight_stops_the_voice_stack_and_evicts_ollama(
    monkeypatch, journal, no_ollama
):
    docker = FakeDocker({"voice": "running", "kokoro": "running"})
    monkeypatch.setattr(gpu, "_docker", docker)

    entry = await gpu.preflight("run-a", containers=["voice", "kokoro"], path=journal)

    assert docker.states == {"voice": "exited", "kokoro": "exited"}
    assert no_ollama == ["called"]
    assert entry["restore"] == ["voice", "kokoro"]


async def test_the_journal_is_written_before_anything_is_stopped(
    monkeypatch, journal, no_ollama
):
    """A crash between the stop and the record leaves the machine mute."""
    seen = {}

    docker = FakeDocker({"voice": "running"})

    def watching_docker(args, timeout=None):
        if args[0] == "stop":
            seen["journal_at_stop_time"] = journal.exists()
        return docker(args, timeout)

    monkeypatch.setattr(gpu, "_docker", watching_docker)
    await gpu.preflight("run-a", containers=["voice"], path=journal)

    assert seen["journal_at_stop_time"] is True


# ------------------------------------------------------------- requirement 4


async def test_restore_starts_only_what_preflight_stopped(monkeypatch, journal, no_ollama):
    """A container the operator had already stopped stays stopped."""
    docker = FakeDocker({"voice": "running", "kokoro": "exited"})
    monkeypatch.setattr(gpu, "_docker", docker)

    entry = await gpu.preflight("run-a", containers=["voice", "kokoro"], path=journal)
    result = await gpu.restore(entry, path=journal)

    assert result["started"] == ["voice"]
    assert docker.states["kokoro"] == "exited"
    assert not journal.exists()


async def test_prior_state_is_captured_before_the_stop_not_after(
    monkeypatch, journal, no_ollama
):
    """Capturing after the stops would read everything as already-down."""
    docker = FakeDocker({"voice": "running", "kokoro": "running"})
    monkeypatch.setattr(gpu, "_docker", docker)

    entry = await gpu.preflight("run-a", containers=["voice", "kokoro"], path=journal)

    assert sorted(entry["restore"]) == ["kokoro", "voice"]


# ------------------------------------------------------------ requirement 13


async def test_a_failed_restore_keeps_the_journal_and_reports_it(
    monkeypatch, journal, no_ollama
):
    """Silence here means a mute machine that looks healthy everywhere."""
    docker = FakeDocker({"voice": "running"})
    docker.start_failures = {"voice"}
    monkeypatch.setattr(gpu, "_docker", docker)
    monkeypatch.setattr(gpu.asyncio, "sleep", _instant)

    entry = await gpu.preflight("run-a", containers=["voice"], path=journal)
    result = await gpu.restore(entry, path=journal)

    assert result["failures"] == ["voice"]
    assert journal.exists()
    assert json.loads(journal.read_text())["restored"] is False


# ------------------------------------------------------------- requirement 5


async def test_a_crashed_trainer_restores_and_is_reported_as_failed(
    monkeypatch, journal, no_ollama
):
    docker = FakeDocker({"voice": "exited", "hive-trainer-run-a": "exited:137"})
    monkeypatch.setattr(gpu, "_docker", docker)
    monkeypatch.setattr(gpu, "evict_until_claimed", _noop_coro)
    messages: list[str] = []

    entry = {"output_name": "run-a", "restore": ["voice"]}
    result = await gpu.watch(entry, notify=messages.append, poll_seconds=0, path=journal)

    assert result["outcome"] == "failed"
    assert result["exit_code"] == 137
    assert docker.states["voice"] == "running"
    assert "failed" in messages[0]


async def test_a_clean_exit_is_reported_as_succeeded(monkeypatch, journal, no_ollama):
    docker = FakeDocker({"voice": "exited", "hive-trainer-run-a": "exited:0"})
    monkeypatch.setattr(gpu, "_docker", docker)
    monkeypatch.setattr(gpu, "evict_until_claimed", _noop_coro)
    messages: list[str] = []

    entry = {"output_name": "run-a", "restore": ["voice"]}
    result = await gpu.watch(entry, notify=messages.append, poll_seconds=0, path=journal)

    assert result["outcome"] == "succeeded"
    assert "succeeded" in messages[0]
    assert "voice is back" in messages[0]


# ------------------------------------------------------------- requirement 6


async def test_reconcile_restores_when_the_trainer_is_gone(monkeypatch, journal, no_ollama):
    docker = FakeDocker({"voice": "exited"})
    monkeypatch.setattr(gpu, "_docker", docker)
    monkeypatch.setattr(gpu, "evict_until_claimed", _noop_coro)
    gpu.write_journal({"output_name": "run-a", "restore": ["voice"]}, journal)

    outcome = await gpu.reconcile(path=journal)

    assert outcome["action"] == "restored"
    assert docker.states["voice"] == "running"
    assert not journal.exists()


async def test_reconcile_waits_when_the_trainer_is_still_alive(
    monkeypatch, journal, no_ollama
):
    docker = FakeDocker({"voice": "exited", "hive-trainer-run-a": "running"})
    monkeypatch.setattr(gpu, "_docker", docker)
    monkeypatch.setattr(gpu, "evict_until_claimed", _noop_coro)
    gpu.write_journal({"output_name": "run-a", "restore": ["voice"]}, journal)

    outcome = await gpu.reconcile(path=journal)

    assert outcome["action"] == "resumed-watch"
    assert docker.states["voice"] == "exited"
    assert journal.exists()


async def test_a_host_with_no_journal_restores_nothing(monkeypatch, journal):
    """Containers the operator stopped themselves are none of our business."""
    docker = FakeDocker({"voice": "exited"})
    monkeypatch.setattr(gpu, "_docker", docker)

    outcome = await gpu.reconcile(path=journal)

    assert outcome["action"] == "nothing-to-do"
    assert docker.states["voice"] == "exited"


async def test_an_unreachable_docker_is_not_read_as_a_finished_run(
    monkeypatch, journal, no_ollama
):
    """Restoring onto a live trainer is the failure this prevents."""
    docker = FakeDocker({"voice": "exited"}, unreachable=True)
    monkeypatch.setattr(gpu, "_docker", docker)
    monkeypatch.setattr(gpu, "evict_until_claimed", _noop_coro)
    gpu.write_journal({"output_name": "run-a", "restore": ["voice"]}, journal)

    outcome = await gpu.reconcile(path=journal)

    assert outcome["action"] == "resumed-watch"
    assert docker.states["voice"] == "exited"


# ------------------------------------------------------------ requirement 11


async def test_a_live_run_is_in_flight_and_a_dead_one_is_not(monkeypatch, journal):
    monkeypatch.setattr(gpu, "JOURNAL_PATH", journal)
    gpu.write_journal({"output_name": "run-a", "restore": []}, journal)

    monkeypatch.setattr(gpu, "_docker", FakeDocker({"hive-trainer-run-a": "running"}))
    assert gpu.run_in_flight(journal) is not None

    monkeypatch.setattr(gpu, "_docker", FakeDocker({"hive-trainer-run-a": "exited:0"}))
    assert gpu.run_in_flight(journal) is None


# --------------------------------------------------------- eviction pumping


async def test_the_eviction_pump_stops_once_the_trainer_has_the_card(monkeypatch):
    """Pumping past the allocation is work against a process that lost."""
    readings = iter([0, 0, 30_000])
    monkeypatch.setattr(gpu, "trainer_vram_mib", lambda: next(readings))
    calls: list[int] = []

    async def fake_evict(url=None):
        calls.append(1)
        return []

    monkeypatch.setattr(gpu, "evict_models", fake_evict)
    monkeypatch.setattr(gpu.asyncio, "sleep", _instant)

    assert await evict_once(gpu) is True
    assert len(calls) == 2


async def evict_once(module):
    return await module.evict_until_claimed(poll_seconds=0, max_seconds=60)


async def test_a_failing_notification_never_blocks_the_restore(monkeypatch):
    def explode(_message):
        raise RuntimeError("telegram is down")

    await gpu.safe_notify(explode, "anything")


async def _instant(_seconds):
    return None


async def _noop_coro(*args, **kwargs):
    return True


# ------------------------------- reclaimable VRAM and watching a run go by


async def test_reclaimable_counts_the_containers_and_ollama(monkeypatch):
    """Requirement: feasibility is judged against post-preflight memory.

    Attribution is by host pid, because from inside a container nvidia-smi
    reports every process name as ``[Not Found]``. A test that matched on
    names would pass against an implementation that cannot work here.
    """

    def fake_docker(args, timeout=None):
        assert args[0] == "top"
        pids = {"hive-mind-voice": "4444", "hive-mind-voice-kokoro": "4876"}
        return _Completed(0, "PID\n" + pids[args[1]] + "\n", "")

    def fake_smi(args, capture_output=None, text=None, timeout=None):
        assert "--query-compute-apps=pid,used_memory" in args
        # The two voice processes, plus two Ollama servers that are not
        # in any container and must not be counted twice.
        return _Completed(
            0,
            "4444, 9974\n4876, 2498\n3331247, 11912\n3331937, 13406\n",
            "",
        )

    async def fake_ps(url=None):
        return 23_898

    monkeypatch.setattr(gpu, "_docker", fake_docker)
    monkeypatch.setattr(gpu.subprocess, "run", fake_smi)
    monkeypatch.setattr(gpu, "ollama_vram_mib", fake_ps)

    total = await gpu.reclaimable_mib(["hive-mind-voice", "hive-mind-voice-kokoro"])

    assert total == 9_974 + 2_498 + 23_898


def test_trainer_logs_answer_a_finished_run_and_an_absent_one(monkeypatch):
    """Requirement: the run can be watched without the watching mattering.

    A trainer that has already exited still has output, which is exactly
    when someone wants to read it, so a stopped container is answered
    rather than refused.
    """
    docker = FakeDocker({"hive-trainer-run-a": "exited:0"})

    def with_logs(args, timeout=None):
        if args[0] == "logs":
            if args[-1] != "hive-trainer-run-a":
                return _Completed(1, "", f"Error: No such container: {args[-1]}")
            return _Completed(0, "step 10 loss 1.2\n\nstep 20 loss 0.9\n", "")
        return docker(args, timeout)

    monkeypatch.setattr(gpu, "_docker", with_logs)

    finished = gpu.trainer_logs("run-a")
    assert finished["available"] is True
    assert finished["lines"] == ["step 10 loss 1.2", "step 20 loss 0.9"]

    missing = gpu.trainer_logs("run-b")
    assert missing["available"] is False
    assert missing["lines"] == []
    assert "no trainer container" in missing["reason"]


# -------------------------------------------- killing a run, and its evidence


async def test_a_killed_run_restores_and_is_recorded_as_cancelled(
    monkeypatch, journal, no_ollama, tmp_path
):
    """Requirement: stopping a run leaves the machine as a finished run would.

    Exit 137 is what both a deliberate stop and an OOM kill look like, so
    the test asserts the outcome word rather than the exit code — an
    implementation inferring intent from the corpse fails here.
    """
    monkeypatch.setattr(gpu, "LOG_ARCHIVE_DIR", tmp_path / "logs")
    docker = FakeDocker({"voice": "exited", "hive-trainer-run-a": "exited:137"})

    def with_logs(args, timeout=None):
        if args[0] == "logs":
            return _Completed(0, "step 40 loss 0.8\n", "")
        return docker(args, timeout)

    monkeypatch.setattr(gpu, "_docker", with_logs)
    monkeypatch.setattr(gpu, "evict_until_claimed", _noop_coro)
    closed: list[tuple] = []
    messages: list[str] = []

    async def close_run(run_id, outcome, exit_code):
        closed.append((run_id, outcome, exit_code))

    entry = {"output_name": "run-a", "run_id": "r1", "restore": ["voice"], "cancelled": True}
    gpu.write_journal(entry, journal)

    result = await gpu.watch(
        entry, notify=messages.append, poll_seconds=0, path=journal, close_run=close_run
    )

    assert result["outcome"] == "cancelled"
    assert docker.states["voice"] == "running"
    assert not journal.exists()
    assert closed == [("r1", "cancelled", 137)]
    assert "cancelled" in messages[0]


def test_a_stop_reports_a_run_that_is_not_there(monkeypatch):
    """Requirement: stopping nothing says so rather than erroring."""
    monkeypatch.setattr(gpu, "_docker", FakeDocker({}))
    assert gpu.stop_trainer("run-a") == {"stopped": False, "reason": "no run in flight"}


def test_a_finished_runs_output_survives_its_container(monkeypatch, tmp_path):
    """Requirement: the logs outlive the container that held them.

    ``docker logs`` is the only copy and ``docker rm`` destroys it, which
    means the run most needing explanation is the one whose evidence has
    just been deleted.
    """
    monkeypatch.setattr(gpu, "LOG_ARCHIVE_DIR", tmp_path / "logs")
    removed: list[str] = []

    def with_logs(args, timeout=None):
        if args[0] == "logs":
            return _Completed(0, "step 40 loss 0.8\nOutOfMemoryError\n", "")
        if args[0] == "rm":
            removed.append(args[-1])
            return _Completed(0, "", "")
        return _Completed(1, "", "Error: No such object: x")

    monkeypatch.setattr(gpu, "_docker", with_logs)

    archived_at = gpu.archive_trainer_logs("run-a", "r1")
    gpu.remove_trainer("run-a")

    assert archived_at
    assert removed == ["hive-trainer-run-a"]
    recovered = gpu.archived_logs("run-a")
    assert recovered["available"] is True
    assert "OutOfMemoryError" in recovered["lines"][-1]


def test_a_download_in_progress_is_visible_rather_than_blank(monkeypatch):
    """Requirement: something is on screen from launch until the run ends.

    Downloads and progress bars redraw one line with carriage returns and
    emit no newline until they finish, so a naive split shows nothing for
    the fifteen minutes that matter most.
    """

    def with_logs(args, timeout=None):
        assert args[0] == "logs"
        return _Completed(
            0,
            "[21:40:02] trainer container is up, reading the job spec\n",
            "model.safetensors:  12%|# | 2.0G/16G\rmodel.safetensors:  38%|### | 6.1G/16G",
        )

    monkeypatch.setattr(gpu, "_docker", with_logs)

    lines = gpu.trainer_logs("run-a")["lines"]

    assert lines[0].endswith("reading the job spec")
    assert any("12%" in line for line in lines)
    assert any("38%" in line for line in lines)


def test_a_live_run_reports_a_heartbeat_and_a_dead_one_does_not(monkeypatch):
    """Requirement: the pane proves the run is alive between log lines.

    Identical text for a minute is indistinguishable from a poller that
    has died, so liveness has to be its own signal rather than inferred
    from output arriving.
    """
    states = {"running": "true 2026-08-05T02:48:28.123456789Z"}

    def fake_docker(args, timeout=None):
        assert args[0] == "inspect"
        if args[-1] == "hive-trainer-run-a":
            return _Completed(0, states["running"] + "\n", "")
        return _Completed(1, "", "Error: No such object")

    def fake_smi(args, capture_output=None, text=None, timeout=None):
        return _Completed(0, "NVIDIA RTX A6000, 43950, 49140, 1600, 98\n", "")

    monkeypatch.setattr(gpu, "_docker", fake_docker)
    monkeypatch.setattr(gpu.subprocess, "run", fake_smi)

    live = gpu.heartbeat("run-a")
    assert live["live"] is True
    assert live["gpu_name"] == "NVIDIA RTX A6000"
    assert live["vram_mib"] == 43_950
    assert live["vram_free_mib"] == 1_600
    assert live["gpu_util_pct"] == 98
    assert live["uptime_seconds"] > 0

    states["running"] = "false 2026-08-05T02:48:28.123456789Z"
    stopped = gpu.heartbeat("run-a")
    assert stopped["live"] is False
    assert stopped["uptime_seconds"] == 0
    # The card is still reported: someone deciding whether to launch is
    # exactly who needs to know what it is holding.
    assert stopped["vram_free_mib"] == 1_600

    assert gpu.heartbeat("run-b")["live"] is False


def test_the_newest_progress_line_is_what_gets_reported():
    """Requirement: the console can say how much time is left.

    The estimate is parsed from what the trainer said rather than
    recomputed from container uptime, because uptime includes a download
    that on a cold cache is most of the first half hour and would put an
    early estimate out by an order of magnitude.
    """
    lines = [
        "[03:14:02] training starts now — a line lands every step",
        "[03:20:11] step 12/377 · 3% · elapsed 6m 09s · eta 3h 07m · loss 1.8021",
        "[03:26:40] step 24/377 · 6% · elapsed 12m 38s · eta 3h 05m · loss 1.6377",
    ]

    found = gpu.progress_from(lines)

    assert found["step"] == 24
    assert found["total_steps"] == 377
    assert found["percent"] == 6
    assert found["eta"] == "3h 05m"
    assert found["loss"] == 1.6377
    # A run that has not reached the training loop claims nothing.
    assert gpu.progress_from(lines[:1]) == {}


def test_a_stale_journal_is_not_a_run_in_flight(monkeypatch, journal):
    """Requirement: a reloaded page attaches to a run that is really there.

    The journal outliving its trainer is the ordinary aftermath of a
    crash, and treating it as live would leave the console permanently
    attached to a run that ended — and permanently refusing new ones.
    """
    gpu.write_journal({"output_name": "run-a", "run_id": "r1", "restore": []}, journal)
    monkeypatch.setattr(gpu, "JOURNAL_PATH", journal)

    monkeypatch.setattr(gpu, "_docker", FakeDocker({"hive-trainer-run-a": "running"}))
    assert gpu.run_in_flight()["output_name"] == "run-a"

    monkeypatch.setattr(gpu, "_docker", FakeDocker({"hive-trainer-run-a": "exited:0"}))
    assert gpu.run_in_flight() is None

    monkeypatch.setattr(gpu, "_docker", FakeDocker({}))
    assert gpu.run_in_flight() is None
