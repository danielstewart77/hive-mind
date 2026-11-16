"""The chief of staff's recreate: it waits for idle, and refuses rather than lose work.

Every docker command the tool runs goes through `subprocess.run`, the
transport, which these replace with a fake answering per command; the state
directory is moved under a temporary root. Nothing the behaviour lives in is
stubbed: the judgement, the wait, the run and the arm are the real ones.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

TOOL = (Path(__file__).resolve().parents[2]
        / "tools" / "stateless" / "stash_recreate" / "stash_recreate.py")
WATCH = (Path(__file__).resolve().parents[2]
         / "tools" / "staff_watch" / "staff_watch.py")

STASH = "3d3ae243-989b-41f0-87bd-5a79ef42c70c"
CONV = "af6953e6-ca36-484d-a58d-f737c9510f5c"
OTHER_SESSION = "29a7b90f-3f33-4bab-a47b-a8eda2e9e098"
OWN_PS = "ps -eo pid=,ppid=,stat=,args="


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sr(tmp_path, monkeypatch):
    module = _load("stash_recreate", TOOL)
    state = tmp_path / "minds" / "stash" / "data" / "recreate"
    monkeypatch.setattr(module, "REPO", tmp_path)
    monkeypatch.setattr(module, "STATE_DIR", state)
    monkeypatch.setattr(module, "REQUEST", state / "request")
    monkeypatch.setattr(module, "TAKEN", state / "request.taken")
    monkeypatch.setattr(module, "STATUS", state / "status.json")
    monkeypatch.setattr(module, "LOCK", state / ".lock")
    return module


IDLE_PS = "\n".join([
    "      1       0 Ss   /sbin/docker-init -- /opt/venv/bin/python3 -m minds.mind_server",
    "      7       1 Sl   /opt/venv/bin/python3 -m minds.mind_server",
    f"   5047       7 Sl   node /opt/dsh/apps/cli/lib/bin.js --profile hive --session-id {CONV} --resident",
    f"  10358       0 Rs   {OWN_PS}",
])


# ------------------------------------------------------------- P1: busy or idle


def test_an_idle_chief_has_no_busy_process(sr):
    assert sr.busy_processes(IDLE_PS, OWN_PS) == []


def test_a_tool_call_under_the_conversation_is_busy_and_names_it(sr):
    ps = IDLE_PS + "\n  10054    5047 S    bash -c sleep 120; curl ..."
    reasons = sr.busy_processes(ps, OWN_PS)
    assert any(CONV in r and "tool call" in r for r in reasons)


def test_a_detached_stream_reader_is_busy_and_names_its_session(sr):
    ps = IDLE_PS + (f"\n  10105       1 S    curl -sS -N -X POST "
                    f"http://hive-comms:8424/sessions/{OTHER_SESSION}/message")
    reasons = sr.busy_processes(ps, OWN_PS)
    assert any(OTHER_SESSION in r and "holds the stream" in r for r in reasons)


def test_a_busy_reason_never_copies_a_command_line(sr):
    ps = IDLE_PS + "\n  10400       0 S    curl -H Authorization: Bearer s3cr3t http://x/y"
    (reason,) = sr.busy_processes(ps, OWN_PS)
    assert "s3cr3t" not in reason


def test_a_zombie_is_not_work(sr):
    ps = IDLE_PS + "\n  10060    5047 Z    [bash] <defunct>"
    assert sr.busy_processes(ps, OWN_PS) == []


def test_another_exec_into_the_container_is_busy(sr):
    ps = IDLE_PS + "\n  10400       0 Ss   python3 tools/stateless/staff_ledger/staff_ledger.py list"
    assert len(sr.busy_processes(ps, OWN_PS)) == 1


def test_no_mind_server_means_busy_not_idle(sr):
    ps = "      1       0 Ss   /sbin/docker-init\n" + f"  10358       0 Rs   {OWN_PS}"
    assert sr.busy_processes(ps, OWN_PS)


@pytest.fixture
def watch():
    pytest.importorskip("hive_data",
                        reason="hive_data is not installed in this environment")
    return _load("staff_watch", WATCH)


NOW = 1_800_000_000.0


def _task(task_id, status="in progress", stopped_at=None, woken_at=None):
    return {"id": task_id, "name": f"task {task_id}", "status": status,
            "sessions": ["s-1"], "stopped_at": stopped_at,
            "woken_at": woken_at}


def test_a_turn_in_flight_in_the_chiefs_conversation_is_busy(sr, watch):
    live = {"sessions": [{"session_id": CONV, "mind_id": STASH,
                          "last_block_at": NOW - 5}], "idle": []}
    reasons = sr.judge_watch(watch, live, [], STASH, NOW)
    assert any(CONV in r for r in reasons)


def test_a_task_owed_a_wake_is_busy_and_a_quiet_one_is_not(sr, watch):
    live = {"sessions": [], "idle": []}
    owed = sr.judge_watch(watch, live, [_task("t1", stopped_at=NOW - 60)],
                          STASH, NOW)
    quiet = sr.judge_watch(watch, live, [_task("t2", status="on hold",
                                               stopped_at=NOW - 60)],
                           STASH, NOW)
    assert any("t1" in r for r in owed)
    assert quiet == []


def test_an_idle_feed_and_settled_ledger_are_idle(sr, watch):
    live = {"sessions": [], "idle": [{"session_id": CONV, "mind_id": STASH}]}
    tasks = [_task("t3", stopped_at=NOW - 60, woken_at=NOW - 30)]
    assert sr.judge_watch(watch, live, tasks, STASH, NOW) == []


# ------------------------------------------------------------ the fake docker


class Docker:
    """`subprocess.run`, answering per command; records every command."""

    def __init__(self, answers):
        self.calls: list[list[str]] = []
        self.inputs: list[str] = []
        self.answers = answers

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if kwargs.get("input"):
            self.inputs.append(kwargs["input"])
        line = " ".join(args)
        for key, answer in self.answers.items():
            if key in line:
                out = answer(args) if callable(answer) else answer
                return subprocess.CompletedProcess(args, *out)
        return subprocess.CompletedProcess(args, 0, "", "")

    def ran(self, fragment):
        return [c for c in self.calls if fragment in " ".join(c)]


def _config(source="/host/hive-data/hive_data", read_only=True):
    return json.dumps({"services": {"stash": {"volumes": [
        {"target": "/opt/venv/lib/python3.12/site-packages/hive_data",
         "source": source, "read_only": read_only}]}}})


def _probe(missing=None):
    return json.dumps({"file": "x", "missing": {"edit": missing or []}})


def _healthy_docker(over=None):
    answers = {
        "config --format": (0, _config(), ""),
        "config --images": (0, "hive_mind-stash\n", ""),
        "image inspect": (0, "sha256:old\n", ""),
        "docker run": (0, _probe(), ""),
        "exec hive-mind-stash ps": (0, IDLE_PS, ""),
        "watch-check": (0, json.dumps({"reasons": []}), ""),
    }
    return Docker({**(over or {}), **{k: v for k, v in answers.items()
                                      if k not in (over or {})}})


def test_an_unanswerable_watch_is_busy(sr, monkeypatch):
    docker = _healthy_docker({"watch-check": (1, "", "no such container")})
    monkeypatch.setattr(subprocess, "run", docker)
    reasons = sr.check_idle()
    assert any("watch could not say" in r for r in reasons)


# -------------------------------------------- P2: never idle, never recreated


def _never_idle_wait(sr, reasons=("a turn is in flight in conversation x",)):
    clock = iter(range(0, 10_000, 30)).__next__
    return functools.partial(sr.wait_for_idle, check=lambda: list(reasons),
                             clock=clock, sleep=lambda s: None)


def test_a_chief_never_idle_before_the_deadline_is_refused_not_recreated(
        sr, monkeypatch):
    docker = _healthy_docker()
    monkeypatch.setattr(subprocess, "run", docker)
    out = sr.run("test", deadline=300, wait=_never_idle_wait(sr))
    assert out["state"] == "refused"
    assert out["recreated"] is False
    assert docker.ran("compose up") == []
    assert "a turn is in flight in conversation x" in out["busy"]


def test_a_refusal_after_the_build_puts_the_old_image_tag_back(
        sr, monkeypatch):
    ids = iter(["sha256:old\n", "sha256:new\n"])
    docker = _healthy_docker({"image inspect": lambda a: (0, next(ids, "sha256:new\n"), "")})
    monkeypatch.setattr(subprocess, "run", docker)
    sr.run("test", deadline=300, wait=_never_idle_wait(sr))
    assert docker.ran("tag sha256:old hive_mind-stash")


def test_one_busy_check_between_two_idle_ones_waits_again(sr):
    answers = iter([[], ["process 9 is running"], [], []])
    run = sr.Run("test")
    sr.wait_for_idle(run, 300, check=lambda: next(answers),
                     clock=lambda: 0.0, sleep=lambda s: None)
    assert run.data["busy"] == ["process 9 is running"]


# ---------------------------------------- P3: a failed preflight never recreates


def test_a_failed_build_is_refused_and_not_recreated(sr, monkeypatch):
    docker = _healthy_docker({"compose build": (1, "", "build exploded")})
    monkeypatch.setattr(subprocess, "run", docker)
    out = sr.run("test", deadline=300, wait=_never_idle_wait(sr, ()))
    assert out["state"] == "refused"
    assert "build exploded" in out["error"]
    assert docker.ran("compose up") == []


def test_a_hive_data_behind_the_ledger_tool_is_refused_and_not_recreated(
        sr, monkeypatch):
    docker = _healthy_docker({"docker run": (0, _probe(["update_task(question=)"]), "")})
    monkeypatch.setattr(subprocess, "run", docker)
    out = sr.run("test", deadline=300, wait=_never_idle_wait(sr, ()))
    assert out["state"] == "refused"
    assert "update_task(question=)" in out["error"]
    assert docker.ran("compose up") == []


def test_a_fragment_without_the_mount_is_refused(sr, monkeypatch):
    docker = _healthy_docker({"config --format": (0, json.dumps(
        {"services": {"stash": {"volumes": []}}}), "")})
    monkeypatch.setattr(subprocess, "run", docker)
    out = sr.run("test", deadline=300, wait=_never_idle_wait(sr, ()))
    assert out["state"] == "refused"
    assert docker.ran("compose up") == []


def test_the_probe_container_has_no_network_and_no_chief_credential(
        sr, monkeypatch):
    docker = _healthy_docker()
    monkeypatch.setattr(subprocess, "run", docker)
    sr.probe_image("hive_mind-stash", "/host/hive-data/hive_data")
    (call,) = docker.ran("docker run")
    assert call[call.index("--network") + 1] == "none"
    assert not [a for a in call if a.startswith("-e") or "TOKEN" in a]


# ------------------------------------------------------------------ P4: arm


def test_arm_withdraws_a_request_nothing_took(sr):
    clock = iter(range(0, 1000)).__next__
    with pytest.raises(sr.Refused, match="did not take"):
        sr.arm("test", within=5, clock=clock, sleep=lambda s: None)
    assert not sr.REQUEST.exists()


def test_arm_reports_a_request_the_host_took(sr):
    def host_takes(_):
        sr.REQUEST.unlink(missing_ok=True)
    out = sr.arm("test", within=5, clock=iter(range(1000)).__next__,
                 sleep=host_takes)
    assert out["armed"] is True


def test_arm_refuses_while_a_run_is_active(sr):
    sr.Run("first")  # writes an active, fresh status
    with pytest.raises(sr.Refused, match="already"):
        sr.arm("second", within=5)


def test_arm_treats_a_silent_run_as_dead(sr):
    record = sr.Run("crashed")
    record.data["updated_at"] = 0.0
    sr._write_json(sr.STATUS, record.data)

    def host_takes(_):
        sr.REQUEST.unlink(missing_ok=True)
    out = sr.arm("again", within=5, sleep=host_takes)
    assert out["armed"] is True


def test_the_run_consumes_the_request_the_unit_moved_aside(sr):
    sr._write_json(sr.TAKEN, {"reason": "bubbles", "deadline": 60})
    taken = sr.take_request()
    assert taken["reason"] == "bubbles"
    assert not sr.TAKEN.exists()


# ------------------------------------------------------- P5: the verification


class Ledger:
    """The recreated container's ledger tool, as its stdout."""

    def __init__(self, tasks, *, sticks=True, clears=True, replaced_with=None):
        self.tasks = {t["id"]: dict(t) for t in tasks}
        self.sticks = sticks
        self.clears = clears
        self.replaced_with = replaced_with
        self.edited: list[str] = []

    def __call__(self, args):
        i = args.index("/usr/src/app/tools/stateless/staff_ledger/staff_ledger.py")
        command, rest = args[i + 1], args[i + 2:]
        if command == "list":
            return 0, json.dumps({"tasks": list(self.tasks.values())}), ""
        task = self.tasks[rest[0]]
        if command == "edit":
            self.edited.append(rest[0])
        if command == "edit" and "--question" in rest and self.sticks:
            task["question"] = (self.replaced_with
                                or rest[rest.index("--question") + 1])
        if command == "edit" and "--answered" in rest and self.clears:
            task["question"] = None
        return 0, json.dumps({"task": task}), ""


def _verify_docker(ledger, mounts):
    return _healthy_docker({
        "{{.State.StartedAt}}": (0, "2026-11-15T19:00:00Z\n", ""),
        "{{.State.Running}}": (0, "true\n", ""),
        "{{json .Mounts}}": (0, json.dumps(mounts), ""),
        "curl": (0, json.dumps({"ok": True}), ""),
        "staff_ledger.py": ledger,
        "exec hive-mind-stash /opt/venv/bin/python3 -c": (0, _probe(), ""),
    })


GOOD_MOUNT = [{"Destination": "/opt/venv/lib/python3.12/site-packages/hive_data",
               "Source": "/host/hive-data/hive_data", "RW": False}]


def _verify(sr, monkeypatch, ledger, mounts=GOOD_MOUNT, over=None,
            clock=lambda: 0.0):
    docker = _verify_docker(ledger, mounts)
    docker.answers.update(over or {})
    monkeypatch.setattr(subprocess, "run", docker)
    record = sr.Run("test")
    ok = sr.verify(record, "before", "/host/hive-data/hive_data", [],
                   clock=clock, sleep=lambda s: None)
    return ok, {c["check"]: c["ok"] for c in record.data["checks"]}


def test_verification_passes_when_the_mount_and_question_round_trip_hold(
        sr, monkeypatch):
    ok, checks = _verify(sr, monkeypatch,
                         Ledger([{"id": "t1", "status": "done", "question": None}]))
    assert ok is True
    assert checks["edit --question sets the question"] is True
    assert checks["edit --answered clears it"] is True


def test_verification_fails_when_the_mount_is_absent(sr, monkeypatch):
    ok, checks = _verify(sr, monkeypatch,
                         Ledger([{"id": "t1", "status": "done", "question": None}]),
                         mounts=[])
    assert ok is False
    assert checks["hive_data mounted read-only from the host"] is False


def test_verification_fails_when_the_question_does_not_stick(sr, monkeypatch):
    ok, checks = _verify(sr, monkeypatch, Ledger(
        [{"id": "t1", "status": "done", "question": None}], sticks=False))
    assert ok is False
    assert checks["edit --question sets the question"] is False


def test_a_real_question_asked_meanwhile_is_never_cleared(sr, monkeypatch):
    ledger = Ledger([{"id": "t1", "status": "done", "question": None}],
                    replaced_with="Daniel, which branch?")
    docker = _verify_docker(ledger, GOOD_MOUNT)
    monkeypatch.setattr(subprocess, "run", docker)
    sr.verify(sr.Run("test"), "before", "/host/hive-data/hive_data", [],
              clock=lambda: 0.0, sleep=lambda s: None)
    assert docker.ran("--answered") == []
    assert ledger.tasks["t1"]["question"] == "Daniel, which branch?"


def test_a_wake_marked_during_the_recreate_is_handed_back(sr):
    tasks = [{"id": "a", "name": "A", "woken_at": 100.0},
             {"id": "b", "name": "B", "woken_at": 50.0},
             {"id": "c", "name": "C", "woken_at": None}]
    assert [t["id"] for t in sr.woken_since(tasks, 90.0)] == ["a"]
    assert [t["id"] for t in sr.woken_since(tasks, 100.0)] == ["a"]


FREE = [{"id": "t1", "status": "done", "question": None}]


def test_verification_fails_on_a_writable_mount(sr, monkeypatch):
    mount = [{**GOOD_MOUNT[0], "RW": True}]
    ok, checks = _verify(sr, monkeypatch, Ledger(FREE), mounts=mount)
    assert checks["hive_data mounted read-only from the host"] is False


def test_verification_fails_on_a_mount_from_another_path(sr, monkeypatch):
    mount = [{**GOOD_MOUNT[0], "Source": "/elsewhere/hive_data"}]
    ok, checks = _verify(sr, monkeypatch, Ledger(FREE), mounts=mount)
    assert checks["hive_data mounted read-only from the host"] is False


def test_verification_fails_and_names_the_task_when_the_clear_does_not_hold(
        sr, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _verify_docker(
        Ledger(FREE, clears=False), GOOD_MOUNT))
    record = sr.Run("test")
    ok = sr.verify(record, "before", "/host/hive-data/hive_data", [],
                   clock=lambda: 0.0, sleep=lambda s: None)
    assert ok is False
    assert record.data["marker_left_on"] == "t1"


def test_a_task_already_holding_a_question_is_never_probed(sr, monkeypatch):
    ledger = Ledger([{"id": "t0", "status": "done", "question": "real one"},
                     {"id": "t1", "status": "in progress", "question": None}])
    _verify(sr, monkeypatch, ledger)
    assert set(ledger.edited) == {"t1"}
    assert ledger.tasks["t0"]["question"] == "real one"


def test_verification_fails_when_the_mind_server_never_answers(sr, monkeypatch):
    ticks = iter(range(0, 10_000, 10)).__next__
    ok, checks = _verify(sr, monkeypatch, Ledger(FREE),
                         over={"curl": (7, "", "connection refused")},
                         clock=ticks)
    assert ok is False
    assert checks["mind server /health ok"] is False


def test_verification_fails_when_the_recreated_container_has_an_old_library(
        sr, monkeypatch):
    ok, checks = _verify(sr, monkeypatch, Ledger(FREE), over={
        "exec hive-mind-stash /opt/venv/bin/python3 -c":
            (0, _probe(["start_agent"]), "")})
    assert ok is False


# ------------------------------------------------ the run, end to end, faked


def _run_docker(sr, probes=None, woken=()):
    state = {"up": False}
    probes = iter(probes or [_probe(), _probe()])

    def started(_):
        return 0, ("2026-11-15T20:00:00Z" if state["up"] else
                   "2026-11-15T15:46:20Z") + "\n", ""

    def up(_):
        state["up"] = True
        return 0, "", ""

    def ps(_):
        text = IDLE_PS.replace(f"--session-id {CONV}", f"--resume {CONV}") \
            if state["up"] else IDLE_PS
        return 0, text, ""

    def watch(args):
        if "--since" in args:
            return 0, json.dumps({"reasons": [], "woken": list(woken)}), ""
        return 0, json.dumps({"reasons": []}), ""

    ledger = Ledger(FREE)
    docker = _verify_docker(ledger, GOOD_MOUNT)
    docker.answers.update({
        "compose up": up, "{{.State.StartedAt}}": started,
        "exec hive-mind-stash ps": ps, "watch-check": watch,
        "docker run": lambda a: (0, next(probes), ""),
        "deliver": (0, json.dumps({"delivered": True, "session": CONV,
                                   "turn_completed": True}), "")})
    return docker, state


def test_an_idle_chief_is_recreated_once_with_no_deps_and_reported(
        sr, monkeypatch):
    docker, _ = _run_docker(sr, woken=[{"id": "t9", "name": "lost wake"}])
    monkeypatch.setattr(subprocess, "run", docker)
    out = sr.run("bubbles", deadline=300, wait=lambda *a: None)
    (up,) = docker.ran("compose up")
    assert up[-4:] == ["-d", "--no-deps", "--force-recreate", "stash"][-4:]
    assert "--no-deps" in up and up[-1] == "stash"
    assert out["state"] == "done", out
    assert out["recreated"] is True
    (report,) = docker.inputs
    assert "t9" in report and "recreated." in report


def test_a_recreate_whose_harness_did_not_resume_is_not_done(sr, monkeypatch):
    docker, _ = _run_docker(sr)
    docker.answers["exec hive-mind-stash ps"] = (0, IDLE_PS, "")
    monkeypatch.setattr(subprocess, "run", docker)
    out = sr.run("bubbles", deadline=300, wait=lambda *a: None)
    assert out["state"] == "failed"


def test_a_hive_data_behind_at_preflight_never_waits(sr, monkeypatch):
    waited = []
    docker, _ = _run_docker(sr, probes=[_probe(["update_task(question=)"])])
    monkeypatch.setattr(subprocess, "run", docker)
    out = sr.run("test", deadline=300, wait=lambda *a: waited.append(a))
    assert out["state"] == "refused"
    assert waited == []


def test_a_hive_data_that_falls_behind_during_the_wait_is_refused(
        sr, monkeypatch):
    ids = iter(["sha256:old\n", "sha256:new\n"])
    docker, _ = _run_docker(sr, probes=[_probe(), _probe(["start_agent"])])
    docker.answers["image inspect"] = (
        lambda a: (0, next(ids, "sha256:new\n"), ""))
    monkeypatch.setattr(subprocess, "run", docker)
    out = sr.run("test", deadline=300, wait=lambda *a: None)
    assert out["state"] == "refused"
    assert "fell behind" in out["error"]
    assert docker.ran("compose up") == []
    assert docker.ran("tag sha256:old hive_mind-stash")


def test_work_that_starts_during_the_last_probe_holds_the_recreate(
        sr, monkeypatch):
    docker, _ = _run_docker(sr, probes=[_probe()] * 3)
    monkeypatch.setattr(subprocess, "run", docker)
    answers = iter([["process 12 (curl, parent 1) is running"], []])
    record = sr.Run("test")
    sr.recreate(record, True, 300, lambda *a: None,
                check=lambda: next(answers), clock=lambda: 0.0)
    assert record.data["busy"] == ["process 12 (curl, parent 1) is running"]
    assert len(docker.ran("compose up")) == 1


def test_a_second_run_while_one_holds_the_lock_is_refused(sr, monkeypatch):
    import fcntl
    sr.STATE_DIR.mkdir(parents=True)
    with open(sr.LOCK, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(sr.Refused, match="lock"):
            sr.run("second", deadline=300, wait=lambda *a: None)


@pytest.mark.parametrize("bad", [float("nan"), 0, -5, 10 ** 9, "soon"])
def test_arm_refuses_a_deadline_the_unit_cannot_honour(sr, bad):
    with pytest.raises(sr.Refused, match="deadline"):
        sr.arm("test", deadline=bad, within=1)
    assert not sr.REQUEST.exists()


def test_arm_hands_its_deadline_to_the_run(sr):
    seen = {}

    def host_takes(_):
        if sr.REQUEST.exists():
            seen.update(json.loads(sr.REQUEST.read_text()))
            sr.REQUEST.unlink()
    sr.arm("test", deadline=600, within=5, sleep=host_takes)
    assert seen["deadline"] == 600


def test_arm_refuses_while_a_run_is_in_a_long_quiet_step(sr):
    record = sr.Run("building")
    record.state("building", quiet_for=1800)
    record.data["updated_at"] = record.clock() - 1000
    sr._write_json(sr.STATUS, record.data)
    with pytest.raises(sr.Refused, match="already"):
        sr.arm("second", within=1)


def test_a_taken_request_whose_run_died_does_not_block_arm(sr):
    sr._write_json(sr.TAKEN, {"reason": "old"})

    def host_takes(_):
        sr.REQUEST.unlink(missing_ok=True)
    assert sr.arm("again", within=5, sleep=host_takes)["armed"] is True
