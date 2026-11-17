#!/usr/bin/env python3
"""Recreate the chief of staff's container, only when nothing in it is lost.

usage: stash_recreate.py arm --reason TEXT [--deadline S]   (inside the container, or the host)
       stash_recreate.py run [--from-request] [--reason T] [--deadline S] [--no-build]   (host)
       stash_recreate.py check                   (host: is the chief idle, and if not why)
       stash_recreate.py install                 (host: the systemd user units, no sudo)

The container cannot recreate itself: whatever runs the recreate inside it
dies with it. So the work happens on the host, in a systemd user service that
a path unit starts when a request file appears on the chief's read-write
mount, `minds/stash/data/recreate/request`. `arm` writes that file, from
either side, and says whether the host took it. Whoever arms it from inside a
turn ends that turn next: the turn's own stream is work in flight, and the
run waits for it.

What dies with the container, and so is waited for:

- A turn in flight. The resident harness process holds it.
- A dispatched turn on another mind. `workspace/bin/dispatch.sh` reads its
  stream with a detached curl in this container, and the gateway runs a turn
  only while a reader holds its stream, so killing the reader loses the reply.
- A tool call, a sleeping poll, a terminal pane, another `docker exec`.
- A wake the watch owes, which would be delivered into a container that is
  not there.

What survives: everything under `minds/stash` (the harness's session logs,
the mind's data and workspace) is a read-write bind mount, so a conversation
resumes from its own log on its next message. The image, `/tmp` and the
processes do not.

Every answer is JSON on stdout. A run's progress, every check and every
reason it waited are kept in `minds/stash/data/recreate/status.json`, which
the chief reads from inside once it is back; the outcome is also delivered to
it as one turn, in the conversation the watch would wake.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

#: The hive_mind checkout this file sits in: the host's, or the container's
#: mount of it at /usr/src/app. The request and status paths are the same
#: relative path on both sides, which is what lets `arm` run on either.
REPO = Path(__file__).resolve().parents[3]
STATE_DIR = REPO / "minds" / "stash" / "data" / "recreate"
REQUEST = STATE_DIR / "request"
#: Where the service's ExecStartPre moves the request before the run starts,
#: so a run that fails at any point cannot leave it to retrigger the unit.
TAKEN = STATE_DIR / "request.taken"
STATUS = STATE_DIR / "status.json"
LOCK = STATE_DIR / ".lock"

SERVICE = "stash"
CONTAINER = "hive-mind-stash"
WATCH = "hive-mind-stash-watch"
STASH_MIND_ID = "3d3ae243-989b-41f0-87bd-5a79ef42c70c"

#: Where both containers keep this checkout, and where the fragment mounts
#: the host's hive_data over the copy the image baked.
APP = "/usr/src/app"
TOOL_IN_CONTAINER = f"{APP}/tools/stateless/stash_recreate/stash_recreate.py"
LEDGER_DIR = "tools/stateless/staff_ledger"
LEDGER_IN_CONTAINER = f"{APP}/{LEDGER_DIR}/staff_ledger.py"
SESSIONS = "minds/stash/.dsh/sessions"
MOUNT_DEST = "/opt/venv/lib/python3.12/site-packages/hive_data"
PYTHON = "/opt/venv/bin/python3"

#: States a run ends in. Any other state is a run still going.
TERMINAL = frozenset({"done", "refused", "failed"})

#: How long a run may go without touching status.json before `arm` treats it
#: as dead rather than active. A waiting run writes on every check.
HEARTBEAT = 180.0

DEADLINE = 2 * 60 * 60.0
#: The longest wait a request may ask for. The unit's TimeoutStartSec is this
#: plus every other step's own bound, so systemd never kills a run between
#: removing the old container and starting the new one.
MAX_DEADLINE = 8 * 60 * 60.0
BUILD_TIMEOUT = 1800.0
INTERVAL = 20.0
#: The second idle check, which must agree with the first.
SETTLE = 5.0
HEALTH_WITHIN = 90.0
#: How long `arm` waits for the host to take its request.
TAKEN_WITHIN = 15.0
#: How long the outcome's turn may run. A turn, not a request.
TURN_TIMEOUT = 1800.0
#: Everything a run does besides the wait, bounded: build, two probes, the
#: recreate, the verification and the report turn, with room to spare.
UNIT_TIMEOUT = MAX_DEADLINE + BUILD_TIMEOUT + TURN_TIMEOUT + 2 * 60 * 60.0

PROBE_QUESTION = ("Recreate check by stash_recreate, run {run}: this question "
                  "is set and cleared within seconds. Ignore it.")

#: Read inside a container: what the ledger tool finds absent from the
#: hive_data it loaded, per command, and where that library is.
_MISSING_PROBE = (
    "import json, sys; sys.path.insert(0, %r); import staff_ledger as l; "
    "import hive_data; print(json.dumps({'file': getattr(hive_data, "
    "'__file__', None), 'missing': {c: l.missing(c) for c in l.NEEDS}}))"
    % f"{APP}/{LEDGER_DIR}")

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_STREAM = re.compile(rf"/sessions/({_UUID})/message")
_CONVERSATION = re.compile(rf"--(?:session-id|resume)[ =]({_UUID})")
_STARTED = re.compile(rf"--(session-id|resume)[ =]({_UUID})")


class Refused(Exception):
    """A step that would have lost work, or could not prove it would not."""


def checked_deadline(value: Any) -> float:
    """A wait the unit can finish inside its own timeout, or Refused."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise Refused(f"deadline {value!r} is not a number of seconds") from None
    if not (0 < seconds <= MAX_DEADLINE):  # also refuses nan
        raise Refused(f"deadline {value!r} must be more than 0 and at most "
                      f"{MAX_DEADLINE:.0f} seconds")
    return seconds


# ------------------------------------------------------------- the transport


def _cmd(args: list[str], *, timeout: float = 120.0,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    """Every process this tool starts. The one seam tests replace."""
    try:
        return subprocess.run(args, cwd=REPO, input=stdin, text=True,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 124, "", f"timed out after {timeout:.0f}s")
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def _tail(done: subprocess.CompletedProcess, size: int = 400) -> str:
    text = (done.stderr or "").strip() or (done.stdout or "").strip()
    return text[-size:] or f"exit {done.returncode}"


def _json_out(done: subprocess.CompletedProcess) -> Any:
    """The last JSON line a command printed, or None."""
    for line in reversed((done.stdout or "").strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


# ------------------------------------------------------------ the status file


def _write_json(path: Path, data: dict) -> None:
    """Temp file and rename, so a reader never sees half a record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    os.replace(tmp, path)


def read_status() -> dict | None:
    try:
        return json.loads(STATUS.read_text())
    except (OSError, json.JSONDecodeError):
        return None


class Run:
    """One run's record, written to status.json on every change."""

    def __init__(self, reason: str, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.data: dict[str, Any] = {
            "run": uuid.uuid4().hex[:12], "state": "starting",
            "reason": reason, "started_at": clock(), "checks": [],
            "busy": [], "recreated": False}
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = self.clock()
        _write_json(STATUS, self.data)

    def state(self, state: str, *, quiet_for: float = 0.0,
              **extra: Any) -> None:
        """Move to `state`. `quiet_for` is how long the next step may run
        without writing, so `arm` does not read a silent step as a dead run."""
        self.data.update(state=state, active_until=self.clock() + quiet_for,
                         **extra)
        self.save()

    def check(self, name: str, ok: bool | None, detail: Any = "") -> bool:
        """Record one check. `ok=None` is skipped: neither held nor failed."""
        self.data["checks"].append({
            "check": name, "ok": None if ok is None else bool(ok),
            "detail": detail})
        self.save()
        return bool(ok)

    def busy(self, reasons: list[str]) -> None:
        seen = self.data["busy"]
        for reason in reasons:
            if reason not in seen:
                seen.append(reason)
        self.data["last_busy"] = reasons
        self.save()

    def passed(self) -> bool:
        """Every check held. A skipped check is not a failure."""
        return all(c["ok"] is not False for c in self.data["checks"])


# ---------------------------------------------------------- is it idle: pure


def _rows(ps_text: str) -> list[tuple[int, int, str, str]]:
    rows = []
    for line in ps_text.strip().splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append((int(parts[0]), int(parts[1]), parts[2],
                     parts[3] if len(parts) > 3 else ""))
    return rows


def conversations(ps_text: str) -> list[str]:
    """The conversations the container's harness processes hold open."""
    return sorted({match.group(1) for _, _, stat, args in _rows(ps_text)
                   if not stat.startswith("Z")
                   for match in [_CONVERSATION.search(args)] if match})


def busy_processes(ps_text: str, own_args: str) -> list[str]:
    """What in the container's process table is work that would be lost.

    `ps_text` is `ps -eo pid=,ppid=,stat=,args=`. Idle is the init, the mind
    server under it, and resident harness processes under the server that
    have no children; anything else is a turn's tool call, a detached stream
    reader, a poll, a terminal pane or another `docker exec`. A zombie is
    not work. `own_args` is the ps that produced the table, which is not.
    """
    rows = _rows(ps_text)
    if not rows:
        return ["the container's process table could not be read"]
    servers = {pid for pid, ppid, _, args in rows
               if ppid == 1 and "minds.mind_server" in args}
    if not servers:
        return ["the mind server is not running in the container, so it "
                "cannot say whether a turn is in flight"]
    residents = {pid for pid, ppid, _, args in rows
                 if ppid in servers and "--resident" in args}
    parents = {ppid for _, ppid, stat, _ in rows if not stat.startswith("Z")}
    reasons = []
    for pid, ppid, stat, args in rows:
        if stat.startswith("Z") or pid == 1 or pid in servers:
            continue
        if ppid == 0 and args.strip() == own_args:
            continue
        if pid in residents:
            if pid in parents:
                held = _CONVERSATION.search(args)
                reasons.append(
                    "conversation "
                    + (held.group(1) if held else f"process {pid}")
                    + " has a tool call running")
            continue
        stream = _STREAM.search(args)
        if stream:
            reasons.append(f"process {pid} holds the stream of a turn in "
                           f"session {stream.group(1)}; killing it loses "
                           "that turn's reply")
            continue
        # The name only: a command line can carry a bearer token, and this
        # reason is written to the read-write mount and the journal.
        name = os.path.basename(args.split(None, 1)[0]) if args else "?"
        reasons.append(f"process {pid} ({name}, parent {ppid}) is running")
    return reasons


def judge_watch(watch: Any, live: dict, tasks: list[dict], mind_id: str,
                now: float) -> list[str]:
    """What the live feed and the ledger say would be lost, per the watch.

    `watch` is the staff_watch module, so "mid-turn" and "owed a wake" mean
    exactly what they mean to the process that delivers wakes. Read only.
    """
    plan = watch.decide(live, tasks, mind_id, now)
    reasons = []
    if plan["mid_turn"]:
        generating, _ = watch.live_state(live)
        mine = sorted(sid for sid, entry in generating.items()
                      if entry.get("mind_id") == mind_id)
        reasons.append("a turn is in flight in conversation "
                       + ", ".join(mine))
    owed = [task for task in tasks if task["id"] in plan["pending"]
            and task.get("status") not in watch.QUIET]
    for task in owed:
        reasons.append(f"task {task['id']} ({task.get('name')}) is owed a "
                       "wake the watch has not delivered")
    return reasons


def woken_since(tasks: list[dict], since: float) -> list[dict]:
    """Tasks the watch marked woken at or after `since`: a wake sent then may
    have died with the container, and is marked delivered regardless."""
    return [{"id": t["id"], "name": t.get("name")} for t in tasks
            if t.get("woken_at") is not None
            and float(t["woken_at"]) >= since]


# ------------------------------------------------- is it idle: the side effects


_OWN_PS = ["ps", "-eo", "pid=,ppid=,stat=,args="]


def _ps() -> subprocess.CompletedProcess:
    return _cmd(["docker", "exec", CONTAINER, *_OWN_PS], timeout=30)


def _watch(*args: str) -> Any:
    done = _cmd(["docker", "exec", WATCH, PYTHON, TOOL_IN_CONTAINER, *args],
                timeout=60)
    answer = _json_out(done)
    if done.returncode != 0 or not isinstance(answer, dict) \
            or "error" in answer:
        detail = answer.get("error") if isinstance(answer, dict) else None
        raise Refused(detail or _tail(done, 200))
    return answer


def check_idle() -> list[str]:
    """Every reason the chief is not idle now. Empty means idle.

    A check that cannot be answered is a reason: an unknown is never idle.
    """
    reasons: list[str] = []
    ps = _ps()
    if ps.returncode != 0:
        reasons.append(f"the container's processes could not be read: "
                       f"{_tail(ps, 200)}")
    else:
        reasons += busy_processes(ps.stdout, " ".join(_OWN_PS))
    try:
        reasons += [str(r) for r in _watch("watch-check")["reasons"]]
    except (Refused, KeyError, TypeError) as exc:
        reasons.append("the watch could not say whether a turn is in flight "
                       f"or a wake is owed: {exc}")
    return reasons


def _load_watch():
    path = REPO / "tools" / "staff_watch" / "staff_watch.py"
    spec = importlib.util.spec_from_file_location("staff_watch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _watch_check(since: float | None) -> dict:
    """Run inside the watch's container, which holds the feed and ledger."""
    watch = _load_watch()
    watch._level()
    mind_id = (os.environ.get("MIND_ID") or "").strip() or STASH_MIND_ID
    await watch._connect()
    try:
        live = watch.read_live()
        tasks = await watch.staff.tasks()
        for task in tasks:
            task["sessions"] = [row["session_id"] for row in
                                await watch.staff.task_sessions(task["id"])]
    finally:
        await watch.hive_data.close(drain_timeout=5.0)
    out: dict[str, Any] = {
        "reasons": judge_watch(watch, live, tasks, mind_id, time.time())}
    if since is not None:
        out["woken"] = woken_since(tasks, since)
    return out


def wait_for_idle(run: Run, deadline: float, *,
                  interval: float = INTERVAL, settle: float = SETTLE,
                  check: Callable[[], list[str]] | None = None,
                  clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep) -> None:
    """Return once two checks `settle` apart both find nothing in flight.

    Raises Refused at `deadline` (seconds from now) naming what it saw.
    """
    check = check or check_idle
    ends = clock() + deadline
    while True:
        reasons = check()
        if not reasons:
            sleep(settle)
            reasons = check()
            if not reasons:
                return
        run.busy(reasons)
        if clock() + interval > ends:
            raise Refused(
                f"the chief was not idle within {deadline:.0f}s, so nothing "
                "was recreated; last seen: " + "; ".join(reasons))
        sleep(interval)


# ----------------------------------------------------------------- preflight


def _compose_mount_source() -> str:
    """The host path the fragment mounts over the baked hive_data."""
    done = _cmd(["docker", "compose", "config", "--format", "json", SERVICE],
                timeout=60)
    if done.returncode != 0:
        raise Refused(f"docker compose config failed: {_tail(done)}")
    try:
        volumes = json.loads(done.stdout)["services"][SERVICE]["volumes"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise Refused(f"docker compose config has no {SERVICE} volumes: "
                      f"{exc}") from None
    for volume in volumes:
        if volume.get("target") == MOUNT_DEST:
            if not volume.get("read_only"):
                raise Refused(f"the fragment mounts {MOUNT_DEST} writable")
            return str(volume.get("source"))
    raise Refused(f"the {SERVICE} fragment does not mount the host's "
                  f"hive_data over {MOUNT_DEST}; recreating would not "
                  "bring the library level")


def _image() -> str:
    done = _cmd(["docker", "compose", "config", "--images", SERVICE],
                timeout=60)
    name = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    if done.returncode != 0 or not name:
        raise Refused(f"compose names no image for {SERVICE}: {_tail(done)}")
    return name


def _image_id(image: str) -> str | None:
    done = _cmd(["docker", "image", "inspect", "-f", "{{.Id}}", image],
                timeout=30)
    return done.stdout.strip() if done.returncode == 0 else None


def _behind(answer: Any) -> list[str]:
    """Everything the probe found absent, as `command: name` strings."""
    if not isinstance(answer, dict) or not isinstance(
            answer.get("missing"), dict):
        return ["the probe printed no answer"]
    return [f"{command}: {name}"
            for command, names in sorted(answer["missing"].items())
            for name in names]


def probe_image(image: str, source: str) -> list[str]:
    """What the ledger tool would find absent in a container of `image`.

    A throwaway with no network and nothing mounted but the host's hive_data
    and the ledger tool, both read-only: the service's own `compose run`
    would carry the chief's admin credential and its writable mounts.
    """
    done = _cmd(["docker", "run", "--rm", "--network", "none",
                 "-v", f"{source}:{MOUNT_DEST}:ro",
                 "-v", f"{REPO / LEDGER_DIR}:{APP}/{LEDGER_DIR}:ro",
                 image, PYTHON, "-c", _MISSING_PROBE], timeout=300)
    if done.returncode != 0:
        return [f"the probe container failed: {_tail(done)}"]
    return _behind(_json_out(done))


def preflight(run: Run, build: bool) -> tuple[str, str]:
    """Everything that can be proven before the container is touched.

    Returns the hive_data mount's host source and the image. Raises Refused;
    the caller puts the previous image's tag back on any refusal, so the tag
    still names what the running container was made from.
    """
    source = _compose_mount_source()
    run.check("fragment mounts host hive_data read-only", True, source)
    image = _image()
    if build:
        run.state("building", quiet_for=BUILD_TIMEOUT)
        built = _cmd(["docker", "compose", "build", SERVICE],
                     timeout=BUILD_TIMEOUT)
        if not run.check("image rebuilt", built.returncode == 0,
                         "" if built.returncode == 0 else _tail(built)):
            raise Refused(f"the image did not build: {_tail(built)}")
    behind = probe_image(image, source)
    if not run.check("the new image, with the host's hive_data, has "
                     "everything the ledger tool calls", not behind,
                     behind or "nothing missing"):
        raise Refused(f"hive_data at {source} is behind the ledger tool "
                      f"({'; '.join(behind)}); pull hive-data on the host")
    return source, image


def restore_image(run: Run, image: str) -> None:
    previous = run.data.get("previous_image")
    if not previous or _image_id(image) == previous:
        return
    done = _cmd(["docker", "tag", previous, image], timeout=30)
    run.check(f"{image} tagged back to the image the container runs",
              done.returncode == 0, previous)


# ------------------------------------------------------------------ verifying


def _started_at() -> str | None:
    done = _cmd(["docker", "inspect", "-f", "{{.State.StartedAt}}",
                 CONTAINER], timeout=30)
    return done.stdout.strip() if done.returncode == 0 else None


def _ledger(*args: str) -> Any:
    done = _cmd(["docker", "exec", CONTAINER, PYTHON, LEDGER_IN_CONTAINER,
                 *args], timeout=60)
    return _json_out(done)


def _question(task: str) -> Any:
    shown = _ledger("show", task)
    if not isinstance(shown, dict) or not isinstance(shown.get("task"), dict):
        return ("unreadable", shown)
    return shown["task"].get("question")


def question_round_trip(run: Run) -> None:
    """Set and clear a question on a real task, through the recreated mind.

    The task is one with no question, so nothing Daniel was asked is
    overwritten; a closed one first, so the bubble draws nowhere he is
    looking. The marker names this run, and the clear runs only while the
    question is still the marker, so a real question asked in between is
    never wiped. A marker that could not be cleared is recorded.
    """
    listed = _ledger("list")
    tasks = listed.get("tasks") if isinstance(listed, dict) else None
    if not isinstance(tasks, list):
        run.check("ledger list", False, listed)
        return
    free = [t for t in tasks if not t.get("question")]
    if not free:
        run.check("edit --question and --answered on a real task", None,
                  f"skipped: all {len(tasks)} tasks hold a question")
        return
    free.sort(key=lambda t: t.get("status") != "done")
    task = str(free[0]["id"])
    marker = PROBE_QUESTION.format(run=run.data["run"])
    try:
        _ledger("edit", task, "--question", marker)
        held = _question(task)
        run.check("edit --question sets the question", held == marker,
                  {"task": task, "question": held})
    finally:
        now = _question(task)
        if now == marker:
            _ledger("edit", task, "--answered")
            held = _question(task)
            run.check("edit --answered clears it", held is None,
                      {"task": task, "question": held})
            if held is not None:
                run.data["marker_left_on"] = task
        elif isinstance(now, tuple):
            # Unreadable: the marker may be standing on a real task, and
            # nothing here can say it is not.
            run.check("edit --answered clears it", False,
                      {"task": task, "unreadable": now[1]})
            run.data["marker_left_on"] = task
        else:
            run.check("edit --answered clears it", None,
                      {"task": task, "skipped": "the question is no longer "
                       "the marker, so it is not this run's to clear"})


def logs_read(run: Run, held: list[str]) -> None:
    """Each conversation the old container held reads to its end."""
    for conversation in held:
        found = sorted((REPO / SESSIONS).glob(
            f"*/{conversation}/session.jsonl.zstd"))
        if not found:
            run.check(f"conversation {conversation} log reads to its end",
                      None, "no log on disk (not yet written)")
            continue
        inside = f"{APP}/{found[0].relative_to(REPO)}"
        done = _cmd(["docker", "exec", CONTAINER, "sh", "-c",
                     'node "$DSH_READ_LOG_CLI" "$1" > /dev/null', "sh",
                     inside], timeout=120)
        run.check(f"conversation {conversation} log reads to its end",
                  done.returncode == 0,
                  inside if done.returncode == 0 else _tail(done))


def verify(run: Run, before: str | None, source: str, held: list[str],
           *, clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep) -> bool:
    run.state("verifying")
    started = _started_at()
    running = _cmd(["docker", "inspect", "-f", "{{.State.Running}}",
                    CONTAINER], timeout=30).stdout.strip() == "true"
    run.check("container running and started after the recreate",
              running and started is not None and started != before,
              {"before": before, "after": started, "running": running})

    ends = clock() + HEALTH_WITHIN
    health: Any = None
    while True:
        done = _cmd(["docker", "exec", CONTAINER, "curl", "-sS", "-m", "5",
                     "http://127.0.0.1:8420/health"], timeout=15)
        health = _json_out(done)
        if isinstance(health, dict) and health.get("ok") is True:
            break
        if clock() > ends:
            break
        sleep(3)
    run.check("mind server /health ok",
              isinstance(health, dict) and health.get("ok") is True, health)

    mounts = _cmd(["docker", "inspect", "-f", "{{json .Mounts}}", CONTAINER],
                  timeout=30)
    mount = None
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        mount = next((m for m in json.loads(mounts.stdout)
                      if m.get("Destination") == MOUNT_DEST), None)
    run.check("hive_data mounted read-only from the host",
              mount is not None and mount.get("RW") is False
              and str(mount.get("Source")) == source,
              mount or "no mount at " + MOUNT_DEST)

    probe = _cmd(["docker", "exec", CONTAINER, PYTHON, "-c", _MISSING_PROBE],
                 timeout=60)
    answer = _json_out(probe)
    behind = _behind(answer)
    run.check("hive_data has start_agent, update_task(question=) and every "
              "other call the ledger tool makes", not behind,
              behind or {"file": answer.get("file")})

    question_round_trip(run)
    logs_read(run, held)
    return run.passed()


# ---------------------------------------------------------------- reporting


def deliver(text: str) -> dict:
    """Hand the chief one turn, through the watch's container, read to end."""
    done = _cmd(["docker", "exec", "-i", WATCH, PYTHON, TOOL_IN_CONTAINER,
                 "deliver"], timeout=TURN_TIMEOUT + 60, stdin=text)
    answer = _json_out(done)
    if isinstance(answer, dict):
        return answer
    return {"delivered": False, "error": _tail(done)}


def _deliver_inside(text: str) -> dict:
    """Run inside the watch's container: one turn, held to its end, in the
    conversation the watch itself would wake."""
    watch = _load_watch()
    mind_id = (os.environ.get("MIND_ID") or "").strip() or STASH_MIND_ID
    session = watch.target_session(watch.read_live(), mind_id)
    if session is None:
        return {"delivered": False,
                "error": "the chief has no idle conversation to report into"}
    url, token = watch._comms()
    request = urllib.request.Request(
        f"{url}/sessions/{session}/message",
        data=json.dumps({"content": text}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"}, method="POST")
    last: dict = {}
    try:
        with urllib.request.urlopen(request, timeout=TURN_TIMEOUT) as answer:
            for raw in answer:
                line = raw.decode(errors="replace").strip()
                if line.startswith("data: "):
                    with contextlib.suppress(json.JSONDecodeError):
                        event = json.loads(line[6:])
                        if event.get("type") == "result":
                            last = event
    except (urllib.error.URLError, OSError) as exc:
        return {"delivered": False, "session": session, "error": str(exc)}
    return {"delivered": True, "session": session,
            "turn_completed": bool(last) and not last.get("is_error"),
            "result": str(last.get("result") or "")[:400]}


def _report_text(data: dict) -> str:
    lines = [f"stash_recreate run {data['run']}: the recreate of your "
             f"container ended {data['state']}, and your container was "
             + ("recreated." if data.get("recreated") else "NOT recreated."),
             f"Requested for: {data.get('reason') or 'no reason given'}."]
    if data.get("error"):
        lines.append(f"Why: {data['error']}")
    for check in data.get("checks", []):
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[check["ok"]]
        lines.append(f"{mark} {check['check']}")
    woken = data.get("woken_in_window") or []
    if woken:
        lines.append("The watch marked these tasks woken while your container "
                     "was going down, so that wake may never have reached you: "
                     + "; ".join(f"{t['id']} ({t['name']})" for t in woken)
                     + ". Run /tend-the-ledger for them.")
    if data.get("marker_left_on"):
        lines.append(f"The probe question could not be cleared from task "
                     f"{data['marker_left_on']}; clear it with "
                     "staff_ledger.py edit --answered.")
    lines.append(f"The full record is {STATUS.relative_to(REPO)}. Read it, "
                 "then tell whoever asked for the recreate how it went.")
    return "\n".join(lines)


def resumed(run: Run, report: dict, held: list[str]) -> None:
    """The report turn completed, and no conversation the old container held
    came back without its log.

    Keyed by the harness's conversation id, which is not the gateway's
    session id: a session whose conversation was replaced by a rotation
    armed before the recreate comes back on a new conversation, started
    fresh by design, and that is not a failed resume. On 2026-11-15 the
    first run read exactly that as a failure.
    """
    if not report.get("delivered"):
        run.check("the report turn completed after the recreate", None,
                  "no turn was delivered")
        return
    run.check("the report turn completed after the recreate",
              bool(report.get("turn_completed")),
              {"session": report.get("session"),
               "result": report.get("result")})
    ps = _ps()
    started = {match.group(2): match.group(1)
               for *_, args in _rows(ps.stdout)
               for match in [_STARTED.search(args)] if match}
    back = {c: flag for c, flag in started.items() if c in held}
    fresh = sorted(c for c in started if c not in held)
    if not back:
        run.check("a held conversation resumed from its log", None,
                  {"held": held, "new_since": fresh,
                   "skipped": "no conversation the old container held has "
                              "been spoken to since"})
        return
    run.check("a held conversation resumed from its log",
              all(flag == "resume" for flag in back.values()),
              {"held": back, "new_since": fresh})


# ---------------------------------------------------------------------- run


@contextlib.contextmanager
def _one_run():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("another recreate run holds the lock") from None
        yield


def recreate(record: Run, build: bool, deadline: float,
             wait: Callable[..., None], *,
             check: Callable[[], list[str]] | None = None,
             clock: Callable[[], float] = time.monotonic) -> None:
    check = check or check_idle
    record.state("preflight")
    image = _image()
    record.data["previous_image"] = _image_id(image)
    try:
        source, _ = preflight(record, build)
        ends = clock() + deadline
        while True:
            record.state("waiting for idle")
            wait(record, max(0.0, ends - clock()))
            # Again after the wait: it can be hours, and a peer can move the
            # host's hive-data checkout in that time.
            behind = probe_image(image, source)
            if not record.check("hive_data still level at the recreate",
                                not behind, behind or "nothing missing"):
                raise Refused(f"hive_data at {source} fell behind the "
                              f"ledger tool while waiting "
                              f"({'; '.join(behind)})")
            # The last word is a check made after the probe, so nothing that
            # started while it ran is killed unseen; a wake the watch sends
            # from here on is named in the report.
            window = time.time()
            last = check()
            if not last:
                break
            record.busy(last)
            if clock() >= ends:
                raise Refused(
                    f"the chief was not idle within {deadline:.0f}s, so "
                    "nothing was recreated; last seen: " + "; ".join(last))
    except BaseException:
        restore_image(record, image)
        raise
    ps = _ps()
    held = conversations(ps.stdout) if ps.returncode == 0 else []
    before = _started_at()
    record.state("recreating", quiet_for=900.0, window_from=window,
                 conversations=held)
    try:
        up = _cmd(["docker", "compose", "up", "-d", "--no-deps",
                   "--force-recreate", SERVICE], timeout=600)
        after = _started_at()
        record.data["recreated"] = after is not None and after != before
        if not record.check("compose up --no-deps --force-recreate",
                            up.returncode == 0,
                            "" if up.returncode == 0 else _tail(up)):
            record.state("failed", error=f"compose up failed: {_tail(up)}")
            return
        ok = verify(record, before, source, held)
        record.state("done" if ok else "failed",
                     error=None if ok else "a check after the recreate "
                     "failed; see checks")
    finally:
        with contextlib.suppress(Exception):
            record.data["woken_in_window"] = _watch(
                "watch-check", "--since", str(window))["woken"]


def run(reason: str, *, build: bool = True, deadline: float = DEADLINE,
        wait: Callable[..., None] = wait_for_idle) -> dict:
    """The whole recreate. Refuses rather than recreate when work is lost."""
    with _one_run():
        record = Run(reason)
        try:
            recreate(record, build, deadline, wait)
        except Refused as exc:
            record.state("refused", error=str(exc))
        except Exception as exc:  # a run always ends in a recorded state
            record.state("failed", error=f"{type(exc).__name__}: {exc}")
        outcome = record.data["state"]
        text = _report_text(record.data)
        # Still a run while its report turn is open: a terminal state here
        # would let `arm` request a second run that systemd cannot start.
        record.state("reporting", quiet_for=TURN_TIMEOUT + 120, outcome=outcome)
        record.data["report"] = report = deliver(text)
        if record.data["recreated"]:
            resumed(record, report, record.data.get("conversations") or [])
            if not record.passed() and outcome == "done":
                outcome = "failed"
        record.data["finished_at"] = time.time()
        record.state(outcome)
        return record.data


def take_request() -> dict | None:
    """The request the service's ExecStartPre moved aside, consumed."""
    for path in (TAKEN, REQUEST):
        try:
            text = path.read_text()
            path.unlink()
        except FileNotFoundError:
            continue
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {"reason": str(data)}
        except json.JSONDecodeError:
            return {"reason": text.strip()[:400]}
    return None


# ---------------------------------------------------------------------- arm


def run_active(now: float) -> bool:
    """Whether status.json describes a run that is still going.

    Read from the file alone, because inside the container that is all there
    is: a run not in a terminal state that wrote within the heartbeat, or
    whose current step said it would be quiet for longer.
    """
    status = read_status()
    if not status or status.get("state") in TERMINAL:
        return False
    alive_until = max(float(status.get("updated_at") or 0) + HEARTBEAT,
                      float(status.get("active_until") or 0))
    return now < alive_until


def arm(reason: str, *, deadline: float = DEADLINE,
        within: float = TAKEN_WITHIN,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep) -> dict:
    """Ask the host for a recreate, and say whether it took the request."""
    deadline = checked_deadline(deadline)
    if run_active(clock()):
        status = read_status() or {}
        raise Refused(f"a recreate run is already {status.get('state')} "
                      f"(requested for: {status.get('reason')}); see "
                      f"{STATUS.relative_to(REPO)}")
    if REQUEST.exists():
        raise Refused(f"a recreate request is already waiting at "
                      f"{REQUEST.relative_to(REPO)}")
    # A request the unit moved aside for a run that is no longer active is
    # one whose run died before reading it: it would only block every arm.
    with contextlib.suppress(FileNotFoundError):
        TAKEN.unlink()
    _write_json(REQUEST, {"reason": reason, "deadline": deadline,
                          "at": clock()})
    ends = clock() + within
    while clock() < ends:
        if not REQUEST.exists():
            return {"armed": True, "status": str(STATUS.relative_to(REPO)),
                    "next": (
                        "End your turn now: your own turn is in flight and "
                        "the host waits for it. Once nothing is in flight it "
                        "rebuilds, recreates, verifies, and reports back to "
                        "you as a turn.")}
        sleep(0.5)
    with contextlib.suppress(FileNotFoundError):
        REQUEST.unlink()
    raise Refused(
        f"the host did not take the request within {within:.0f}s, so it was "
        "withdrawn and nothing will be recreated. Either the "
        "stash-recreate.path user unit is not running (on the host: python3 "
        "tools/stateless/stash_recreate/stash_recreate.py install) or the "
        "service failed to start (journalctl --user -u stash-recreate).")


# ------------------------------------------------------------------ install


UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def units() -> dict[str, str]:
    tool = Path(__file__).resolve()
    hours = int(-(-UNIT_TIMEOUT // 3600))
    return {
        "stash-recreate.path": (
            "[Unit]\nDescription=Watch for a request to recreate the chief "
            "of staff's container\n\n[Path]\n"
            f"PathExists={REQUEST}\nMakeDirectory=yes\n"
            "Unit=stash-recreate.service\n\n"
            "[Install]\nWantedBy=default.target\n"),
        "stash-recreate.service": (
            "[Unit]\nDescription=Recreate the chief of staff's container "
            "when it is idle\n\n[Service]\nType=oneshot\n"
            f"WorkingDirectory={REPO}\n"
            f"ExecStartPre=/bin/mv -f {REQUEST} {TAKEN}\n"
            f"ExecStart={sys.executable} {tool} run --from-request\n"
            f"TimeoutStartSec={hours}h\n"),
    }


def install() -> dict:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in units().items():
        (UNIT_DIR / name).write_text(text)
    env = {**os.environ}
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    for args in (["daemon-reload"], ["enable", "--now", "stash-recreate.path"]):
        done = subprocess.run(["systemctl", "--user", *args], env=env,
                              text=True, capture_output=True)
        if done.returncode != 0:
            raise Refused(f"systemctl --user {' '.join(args)}: {_tail(done)}")
    return {"installed": sorted(units()), "dir": str(UNIT_DIR)}


# --------------------------------------------------------------------- main


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stash_recreate.py",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    armed = sub.add_parser("arm", help="request a recreate (either side)")
    armed.add_argument("--reason", required=True)
    armed.add_argument("--deadline", type=float, default=DEADLINE,
                       help="seconds to wait for idle before refusing")
    ran = sub.add_parser("run", help="the recreate itself (host)")
    ran.add_argument("--from-request", action="store_true")
    ran.add_argument("--reason", default="")
    ran.add_argument("--deadline", type=float, default=DEADLINE)
    ran.add_argument("--no-build", dest="build", action="store_false")
    sub.add_parser("check", help="is the chief idle (host)")
    sub.add_parser("install", help="the systemd user units (host)")
    checked = sub.add_parser("watch-check", help=argparse.SUPPRESS)
    checked.add_argument("--since", type=float)
    sub.add_parser("deliver", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "arm":
            out = arm(args.reason, deadline=args.deadline)
        elif args.command == "run":
            reason, deadline = args.reason, checked_deadline(args.deadline)
            if args.from_request:
                taken = take_request()
                if taken is None:
                    print(json.dumps({"ran": False,
                                      "why": "no request to take"}))
                    return 0
                reason = str(taken.get("reason") or "")
                if taken.get("deadline") is not None:
                    deadline = checked_deadline(taken["deadline"])
            out = run(reason, build=args.build, deadline=deadline)
        elif args.command == "check":
            reasons = check_idle()
            out = {"idle": not reasons, "busy": reasons}
        elif args.command == "install":
            out = install()
        elif args.command == "watch-check":
            import asyncio  # noqa: PLC0415
            out = asyncio.run(_watch_check(args.since))
        else:
            out = _deliver_inside(sys.stdin.read())
    except Refused as exc:
        print(json.dumps({"error": str(exc), "kind": "Refused"}))
        return 1
    except Exception as exc:  # every failure is reported, none is raised
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}",
                          "kind": type(exc).__name__}))
        return 1
    print(json.dumps(out, default=str))
    return 0 if out.get("state", "done") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
