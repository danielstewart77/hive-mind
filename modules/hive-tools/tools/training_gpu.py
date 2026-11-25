"""GPU arbitration around a fine-tune run.

This host has one RTX A6000. It serves inference, runs two TTS stacks, and
drives the monitor. A LoRA run needs most of it, so a launch has to take
the card, hold it, and give it back — including when nobody is awake and
including when this process dies in the middle.

Three things make that non-trivial, and each one is why a piece of this
module exists rather than being a couple of ``docker stop`` calls in the
route handler:

**The journal is written before anything is stopped.** The launch happens
inside a request whose CLI subprocess is killed at a fixed timeout, and
this container is redeployed regularly. If the record of what to restore
lived only in memory, one badly-timed restart would leave the machine mute
until someone noticed by talking to it. The journal is on the one writable
mount, fsynced, and written first.

**Ollama cannot be stopped from here.** It is a host systemd unit; this
process is unprivileged in a container with a Docker socket and nothing
else. What is reachable is a per-model unload over HTTP, which frees the
card *now* and prevents nothing — lucent embeds on every memory write, so
the model is back within seconds. The eviction pump therefore keeps
unloading on a short interval, but only until the trainer has visibly
claimed the card. After that the trainer owns the memory and Ollama's next
load simply fails or falls to CPU, which is the slow-at-night the operator
accepted. Pumping past that point would be pointless work against a
process that can no longer win.

**"Gone" and "cannot tell" are different answers.** A Docker socket that
errors is not a dead trainer. Reading it as one restores the voice stack on
top of a live run and reports a completion that did not happen, so an
unreadable daemon holds the watch open instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: Containers that hold VRAM and are expendable for the duration of a run.
#: Overridable because the set is a property of the host, not of training.
YIELD_CONTAINERS = [
    name.strip()
    for name in os.environ.get(
        "TRAINING_YIELD_CONTAINERS", "hive-mind-voice,hive-mind-voice-kokoro"
    ).split(",")
    if name.strip()
]

#: Lives on a writable mount, not in the read-only repo, and not in /tmp —
#: a restart must be able to find it.
JOURNAL_PATH = Path(
    os.environ.get(
        "TRAINING_GPU_JOURNAL",
        "/home/daniel/Storage/Dev/hive_mind/data/gpu_arbitration.json",
    )
)

OLLAMA_URL = os.environ.get("TRAINING_OLLAMA_URL", "http://host.docker.internal:11434")

#: How often the watcher asks whether the trainer is still alive.
POLL_SECONDS = float(os.environ.get("TRAINING_WATCH_POLL_SECONDS", "20"))

#: How often Ollama is re-evicted while the trainer is still loading, and
#: how long that pump runs before giving up on ever seeing an allocation.
EVICT_SECONDS = float(os.environ.get("TRAINING_EVICT_POLL_SECONDS", "10"))
EVICT_MAX_SECONDS = float(os.environ.get("TRAINING_EVICT_MAX_SECONDS", "5400"))

#: A trainer is considered to have claimed the card once it holds this much.
TRAINER_CLAIMED_MIB = int(os.environ.get("TRAINING_CLAIMED_MIB", "4096"))

DOCKER_TIMEOUT = 90

#: How much of a trainer's output a log poll returns by default.
LOG_TAIL_LINES = int(os.environ.get("TRAINING_LOG_TAIL_LINES", "200"))

#: Where a finished run's output is kept once its container is dropped.
LOG_ARCHIVE_DIR = Path(
    os.environ.get(
        "TRAINING_LOG_ARCHIVE_DIR",
        "/home/daniel/Storage/Dev/hive_mind/data/training_logs",
    )
)
MAX_ARCHIVED_LOG_LINES = int(os.environ.get("TRAINING_LOG_ARCHIVE_LINES", "20000"))

#: How long a stopped trainer is given to exit before it is killed. Long
#: enough for torch to release the card, short enough to be a button.
STOP_GRACE_SECONDS = int(os.environ.get("TRAINING_STOP_GRACE_SECONDS", "30"))

STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_MISSING = "missing"
STATE_UNKNOWN = "unknown"


class DockerUnreachable(RuntimeError):
    """The daemon did not answer. Not a fact about any container."""


# ------------------------------------------------------------------ docker


def _docker(args: list[str], timeout: int = DOCKER_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise DockerUnreachable(str(exc)) from exc


def container_state(name: str) -> str:
    """``running``, ``stopped`` or ``missing``. Raises if docker is mute."""
    completed = _docker(["inspect", "--format", "{{.State.Status}}", name])
    if completed.returncode != 0:
        stderr = (completed.stderr or "").lower()
        if "no such object" in stderr or "no such container" in stderr:
            return STATE_MISSING
        raise DockerUnreachable(completed.stderr.strip()[:400])
    status = completed.stdout.strip()
    # 'restarting' and 'paused' are not running, but they are also not a
    # container someone deliberately stopped — treating them as stopped
    # would have restore start something that was already coming up.
    return STATE_RUNNING if status in {"running", "restarting"} else STATE_STOPPED


def stop_container(name: str) -> bool:
    return _docker(["stop", name]).returncode == 0


def start_container(name: str) -> bool:
    return _docker(["start", name]).returncode == 0


def trainer_container(output_name: str) -> str:
    return f"hive-trainer-{output_name}"


def trainer_status(output_name: str) -> dict[str, Any]:
    """Liveness and, once it has exited, how it ended.

    The launcher deliberately does not pass ``--rm``: the container is the
    only place an exit code survives, and a run that is reported without
    one is a run whose failure looks exactly like its success.
    """
    name = trainer_container(output_name)
    completed = _docker(
        ["inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", name]
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").lower()
        if "no such object" in stderr or "no such container" in stderr:
            return {"state": STATE_MISSING, "exit_code": None}
        raise DockerUnreachable(completed.stderr.strip()[:400])
    parts = completed.stdout.split()
    status = parts[0] if parts else ""
    exit_code = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
    if status in {"running", "created", "restarting"}:
        return {"state": STATE_RUNNING, "exit_code": None}
    return {"state": STATE_STOPPED, "exit_code": exit_code}


def remove_trainer(output_name: str) -> None:
    """Drop the exited container once its outcome has been read."""
    try:
        _docker(["rm", "-f", trainer_container(output_name)])
    except DockerUnreachable:
        log.warning("training.gpu.trainer_rm_failed name=%s", output_name)


def trainer_logs(output_name: str, tail: int = LOG_TAIL_LINES) -> dict[str, Any]:
    """Recent output from the trainer, for watching a run go by.

    Read-only and entirely detached from the run: nothing here starts,
    stops or reaps anything, and no caller's absence changes the outcome.
    A container that has exited still has its logs, which is when they
    matter most, so a stopped trainer is answered rather than refused.
    """
    name = trainer_container(output_name)
    try:
        completed = _docker(["logs", "--tail", str(max(1, tail)), name])
    except DockerUnreachable as exc:
        return {"available": False, "reason": str(exc)[:200], "lines": []}
    if completed.returncode != 0:
        stderr = (completed.stderr or "").lower()
        if "no such object" in stderr or "no such container" in stderr:
            return {"available": False, "reason": "no trainer container", "lines": []}
        return {"available": False, "reason": completed.stderr.strip()[:200], "lines": []}
    # The trainer writes progress to stderr and results to stdout; docker
    # hands both back and the order between them is the order they were
    # written, which is the order worth reading.
    merged = (completed.stdout or "") + (completed.stderr or "")
    return {
        "available": True,
        "reason": "",
        "lines": _as_lines(merged),
    }


def _as_lines(text: str) -> list[str]:
    """Split output into lines a reader can see, carriage returns included.

    A progress bar redraws itself with ``\\r`` and emits no newline until
    it completes, so a naive line split leaves a downloading run looking
    like a run producing nothing at all. Each redraw becomes its own
    line here — noisier than a terminal would render it, and the noise is
    the point: it is the difference between visible progress and a blank
    pane.
    """
    return [line for line in text.replace("\r", "\n").splitlines() if line.strip()]


def archive_trainer_logs(output_name: str, run_id: str = "") -> str:
    """Keep a run's output after its container is gone.

    ``docker logs`` is the only copy, and dropping the container drops it.
    A run whose ending needs explaining is precisely the run whose logs
    have just been deleted, so they are written to disk first. Failure to
    archive is logged and never raised: nothing about reporting an
    outcome should depend on a disk write succeeding.
    """
    result = trainer_logs(output_name, tail=MAX_ARCHIVED_LOG_LINES)
    if not result.get("available") or not result.get("lines"):
        return ""
    stamp = run_id or str(int(time.time()))
    target = LOG_ARCHIVE_DIR / f"{output_name}.{stamp}.log"
    try:
        LOG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(result["lines"]) + "\n")
    except OSError as exc:
        log.warning("training.gpu.log_archive_failed name=%s error=%s", output_name, exc)
        return ""
    return str(target)


def archived_logs(output_name: str, tail: int = LOG_TAIL_LINES) -> dict[str, Any]:
    """The newest archived copy of a run's output, if there is one."""
    try:
        candidates = sorted(LOG_ARCHIVE_DIR.glob(f"{output_name}.*.log"))
    except OSError:
        return {"available": False, "reason": "no archived logs", "lines": []}
    if not candidates:
        return {"available": False, "reason": "no archived logs", "lines": []}
    try:
        lines = candidates[-1].read_text().splitlines()
    except OSError as exc:
        return {"available": False, "reason": str(exc)[:200], "lines": []}
    return {
        "available": True,
        "reason": "",
        "archived": True,
        "lines": lines[-max(1, tail) :],
    }


def stop_trainer(output_name: str, timeout_seconds: int = STOP_GRACE_SECONDS) -> dict[str, Any]:
    """Stop a running trainer. Says what it found rather than assuming.

    Stopping something that is not running is reported, not raised: the
    console offers the button whenever a run looks live, and a run that
    ended between the render and the click is the ordinary case rather
    than an error worth showing anyone.
    """
    try:
        status = trainer_status(output_name)
    except DockerUnreachable as exc:
        return {"stopped": False, "reason": f"docker unreachable: {str(exc)[:160]}"}
    if status["state"] != STATE_RUNNING:
        return {"stopped": False, "reason": "no run in flight"}
    try:
        completed = _docker(
            ["stop", "--time", str(max(1, timeout_seconds)), trainer_container(output_name)],
            timeout=max(DOCKER_TIMEOUT, timeout_seconds + 30),
        )
    except DockerUnreachable as exc:
        return {"stopped": False, "reason": f"docker unreachable: {str(exc)[:160]}"}
    if completed.returncode != 0:
        # A trainer holding the GPU can leave its main process unreapable
        # for longer than the daemon's patience, and docker reports that
        # as a failure to stop even though the signals landed. The watcher
        # settles who is right — it is reading the container's real state.
        log.warning(
            "training.gpu.stop_reported_failure name=%s error=%s",
            output_name,
            (completed.stderr or "").strip()[:200],
        )
        return {"stopped": True, "reason": (completed.stderr or "").strip()[:200]}
    return {"stopped": True, "reason": ""}


async def safe_close_run(close_run, run_id: str, outcome: str, exit_code: int | None) -> None:
    """Write the outcome to the ledger. Never take the restore down with it."""
    if not run_id:
        return
    try:
        result = close_run(run_id, outcome, exit_code)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001 - the ledger is across a process boundary
        log.warning("training.gpu.close_run_failed run_id=%s error=%s", run_id, exc)


def heartbeat(output_name: str) -> dict[str, Any]:
    """Proof the run is alive, cheap enough to ask for once a second.

    A trainer can go minutes between log lines, and a pane showing the
    same text for five minutes is indistinguishable from a pane behind a
    dead poller. This is deliberately *not* written into the container's
    log: a line a second would bury the output that matters and inflate
    the archive by two orders of magnitude. It rides on the poll instead,
    where it is read once and never stored.
    """
    # The card is reported whether or not a trainer is on it: an operator
    # watching this pane wants to know what their GPU is doing, and "no
    # container, therefore no numbers" is the least useful moment to go
    # quiet — it is exactly when they are deciding whether to launch.
    beat: dict[str, Any] = {"live": False, "uptime_seconds": 0, **_card_state()}
    try:
        completed = _docker(
            [
                "inspect",
                "--format",
                "{{.State.Running}} {{.State.StartedAt}}",
                trainer_container(output_name),
            ]
        )
    except DockerUnreachable:
        return beat
    if completed.returncode != 0:
        return beat
    parts = completed.stdout.split()
    beat["live"] = bool(parts) and parts[0] == "true"
    if len(parts) > 1 and beat["live"]:
        beat["uptime_seconds"] = _seconds_since(parts[1])
    return beat


#: The trainer's own progress line. Parsed rather than recomputed here:
#: only the trainer knows when training actually began, and a container's
#: uptime includes a download that can be most of the first half hour.
_PROGRESS = re.compile(
    r"step (?P<step>\d+)/(?P<total>\d+) · (?P<percent>\d+)% · "
    r"elapsed (?P<elapsed>.+?) · eta (?P<eta>[^·]+?)(?: · loss (?P<loss>[\d.]+))?$"
)


def progress_from(lines: list[str]) -> dict[str, Any]:
    """The newest progress the trainer reported, if it has reported any.

    Read from the end backwards: the interesting line is the last one,
    and a run near its end has thousands of older ones in front of it.
    """
    for line in reversed(lines):
        match = _PROGRESS.search(line.strip())
        if not match:
            continue
        found = match.groupdict()
        return {
            "step": int(found["step"]),
            "total_steps": int(found["total"]),
            "percent": int(found["percent"]),
            "elapsed": found["elapsed"].strip(),
            "eta": found["eta"].strip(),
            "loss": float(found["loss"]) if found["loss"] else None,
        }
    return {}


def _seconds_since(timestamp: str) -> int:
    """Age of a Docker RFC3339 timestamp, whose fractional part is longer
    than ``fromisoformat`` accepts on every Python before 3.11."""
    cleaned = timestamp.replace("Z", "+00:00")
    if "." in cleaned:
        head, _, tail = cleaned.partition(".")
        fraction, _, offset = tail.partition("+")
        cleaned = f"{head}.{fraction[:6]}+{offset}" if offset else f"{head}.{fraction[:6]}"
    try:
        started = datetime.fromisoformat(cleaned)
    except ValueError:
        return 0
    return max(0, int(datetime.now(started.tzinfo).timestamp() - started.timestamp()))


def _card_state() -> dict[str, Any]:
    """What the card is doing, as the card itself reports it.

    Free memory is taken from nvidia-smi rather than subtracted from the
    total: the driver's own reserve is neither used nor free, and a
    figure derived by arithmetic is a gigabyte optimistic on this card.
    """
    blank = {"gpu_name": "", "vram_mib": 0, "vram_total_mib": 0, "vram_free_mib": 0, "gpu_util_pct": 0}
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return blank
    if completed.returncode != 0:
        return blank
    parts = [p.strip() for p in (completed.stdout.splitlines() or [""])[0].split(",")]
    if len(parts) != 5 or not all(p.isdigit() for p in parts[1:]):
        return blank
    return {
        "gpu_name": parts[0],
        "vram_mib": int(parts[1]),
        "vram_total_mib": int(parts[2]),
        "vram_free_mib": int(parts[3]),
        "gpu_util_pct": int(parts[4]),
    }


def container_vram_mib(names: list[str] | None = None) -> int:
    """VRAM held by the processes inside the named containers.

    Attribution is by pid rather than by process name: from inside a
    container ``nvidia-smi`` reports every compute app's name as
    ``[Not Found]`` (it cannot resolve pids from another namespace) while
    still reporting the host pid and the memory, and ``docker top`` gives
    the host pids of a container. Names would have been the obvious key
    and it is the one that does not work here.
    """
    names = names if names is not None else YIELD_CONTAINERS
    wanted: set[str] = set()
    for name in names:
        try:
            completed = _docker(["top", name, "-eo", "pid"])
        except DockerUnreachable:
            continue
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines()[1:]:
            pid = line.strip()
            if pid.isdigit():
                wanted.add(pid)
    if not wanted:
        return 0
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return 0
    if completed.returncode != 0:
        return 0
    total = 0
    for line in completed.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0] in wanted and parts[1].isdigit():
            total += int(parts[1])
    return total


async def ollama_vram_mib(url: str = OLLAMA_URL) -> int:
    """VRAM Ollama's loaded models are holding, per Ollama itself.

    Its own ``/api/ps`` is the authority here rather than nvidia-smi: the
    models are host processes, so there is no container to attribute them
    to, and ``size_vram`` slightly under-reports against nvidia-smi. That
    is the safe direction — a launch that reclaims a little more than
    promised is not the failure mode worth guarding.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{url}/api/ps")
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("training.gpu.ollama_ps_failed error=%s", exc)
        return 0
    total = 0
    for model in payload.get("models", []) or []:
        size = model.get("size_vram") or 0
        if isinstance(size, (int, float)) and size > 0:
            total += int(size) // (1024 * 1024)
    return total


async def reclaimable_mib(containers: list[str] | None = None) -> int:
    """What a preflight would hand back to a trainer, right now.

    Exactly the two things preflight takes — the yield containers and
    Ollama's loaded models — so the feasibility check and the launch are
    answering the same question. Anything it cannot measure counts as
    zero, which refuses a borderline run rather than promising memory
    that may not arrive.
    """
    return container_vram_mib(containers) + await ollama_vram_mib()


def trainer_vram_mib() -> int:
    """VRAM held by compute processes on the card, in MiB.

    Used only to answer "has the trainer actually allocated yet", which is
    what ends the eviction pump. nvidia-smi being absent answers zero, and
    the pump then runs its full budget rather than stopping early.
    """
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=used_memory", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return 0
    if completed.returncode != 0:
        return 0
    total = 0
    for line in completed.stdout.splitlines():
        value = line.strip()
        if value.isdigit():
            total += int(value)
    return total


# ------------------------------------------------------------------ ollama


async def loaded_models(url: str = OLLAMA_URL) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{url}/api/ps")
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("training.gpu.ollama_ps_failed error=%s", exc)
        return []
    return [
        entry.get("model") or entry.get("name")
        for entry in payload.get("models", [])
        if entry.get("model") or entry.get("name")
    ]


async def evict_models(url: str = OLLAMA_URL) -> list[str]:
    """Unload every currently-resident model. Returns what was evicted.

    ``keep_alive: 0`` on a generate call with no prompt is Ollama's unload
    verb. It frees the card at that instant and reserves nothing — which
    is why the caller pumps it rather than calling it once.
    """
    models = await loaded_models(url)
    evicted: list[str] = []
    if not models:
        return evicted
    async with httpx.AsyncClient(timeout=60) as client:
        for model in models:
            try:
                await client.post(
                    f"{url}/api/generate", json={"model": model, "keep_alive": 0}
                )
                evicted.append(model)
            except httpx.HTTPError as exc:
                log.warning("training.gpu.evict_failed model=%s error=%s", model, exc)
    return evicted


# ----------------------------------------------------------------- journal


def read_journal(path: Path | None = None) -> dict[str, Any] | None:
    path = path or JOURNAL_PATH
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_journal(entry: dict[str, Any], path: Path | None = None) -> None:
    """Persist and fsync, so a crash one line later still leaves a record."""
    path = path or JOURNAL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    with staging.open("w") as handle:
        json.dump(entry, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    staging.replace(path)


def clear_journal(path: Path | None = None) -> None:
    path = path or JOURNAL_PATH
    try:
        path.unlink()
    except OSError:
        pass


def run_in_flight(path: Path | None = None) -> dict[str, Any] | None:
    """The journal entry of a run still holding the card, if there is one.

    A stale journal whose trainer is gone is not in flight — otherwise one
    crashed run would refuse every future launch as "GPU busy" forever.
    """
    entry = read_journal(path)
    if not entry:
        return None
    try:
        status = trainer_status(entry.get("output_name", ""))
    except DockerUnreachable:
        # Cannot tell. Refusing a launch is the recoverable answer;
        # starting a second trainer onto a busy card is not.
        return entry
    return entry if status["state"] == STATE_RUNNING else None


# --------------------------------------------------------------- preflight


async def preflight(
    output_name: str,
    run_id: str = "",
    containers: list[str] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Free the card and record what has to be given back.

    Prior state is captured *before* the first stop, and the journal is
    written before it too: a container the operator had already stopped
    must not be started later on their behalf, and a crash between the
    stop and the record would strand the machine mute.
    """
    containers = containers if containers is not None else YIELD_CONTAINERS
    prior: dict[str, str] = {}
    for name in containers:
        try:
            prior[name] = container_state(name)
        except DockerUnreachable as exc:
            log.warning("training.gpu.state_unknown container=%s error=%s", name, exc)
            prior[name] = STATE_UNKNOWN

    entry = {
        "run_id": run_id,
        "output_name": output_name,
        "started_at": int(time.time()),
        "prior": prior,
        # Only what was running is ours to restore.
        "restore": [name for name, state in prior.items() if state == STATE_RUNNING],
        "restored": False,
    }
    write_journal(entry, path)

    stopped: list[str] = []
    for name in entry["restore"]:
        try:
            if stop_container(name):
                stopped.append(name)
        except DockerUnreachable as exc:
            log.warning("training.gpu.stop_failed container=%s error=%s", name, exc)
    entry["stopped"] = stopped
    entry["evicted"] = await evict_models()
    write_journal(entry, path)
    log.info(
        "training.gpu.preflight output_name=%s stopped=%s evicted=%s",
        output_name,
        stopped,
        entry["evicted"],
    )
    return entry


async def restore(entry: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Give back exactly what preflight took. Retries, then reports.

    A restore that quietly fails is the worst outcome available here: the
    machine stays mute and every surface still looks healthy.
    """
    failures: list[str] = []
    started: list[str] = []
    for name in entry.get("restore", []):
        for attempt in range(3):
            try:
                if start_container(name):
                    started.append(name)
                    break
            except DockerUnreachable as exc:
                log.warning("training.gpu.start_failed container=%s error=%s", name, exc)
            await asyncio.sleep(2 * (attempt + 1))
        else:
            failures.append(name)
    entry["restored"] = True
    entry["restored_at"] = int(time.time())
    entry["restore_failures"] = failures
    if failures:
        # Keep the journal: reconcile on the next start will try again.
        entry["restored"] = False
        write_journal(entry, path)
    else:
        clear_journal(path)
    log.info("training.gpu.restore started=%s failures=%s", started, failures)
    return {"started": started, "failures": failures}


# ------------------------------------------------------------------- watch


async def evict_until_claimed(
    poll_seconds: float = EVICT_SECONDS,
    max_seconds: float = EVICT_MAX_SECONDS,
    claimed_mib: int = TRAINER_CLAIMED_MIB,
) -> bool:
    """Keep Ollama off the card until the trainer has taken it.

    Returns True if an allocation was observed. The window that matters is
    between the unload and the trainer's first CUDA allocation — minutes,
    while the image and the base weights load — because after that the
    trainer holds the memory and Ollama can no longer take it.
    """
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if trainer_vram_mib() >= claimed_mib:
            return True
        await evict_models()
        await asyncio.sleep(poll_seconds)
    return False


async def watch(
    entry: dict[str, Any],
    notify=None,
    poll_seconds: float = POLL_SECONDS,
    path: Path | None = None,
    close_run=None,
) -> dict[str, Any]:
    """Wait for the trainer to end, then restore and report how it ended."""
    output_name = entry.get("output_name", "")
    asyncio.ensure_future(evict_until_claimed())
    exit_code: int | None = None
    while True:
        try:
            status = trainer_status(output_name)
        except DockerUnreachable as exc:
            # Not an answer. Hold the watch rather than restoring onto a
            # trainer that may well still be running.
            log.warning("training.gpu.docker_unreachable error=%s", exc)
            await asyncio.sleep(poll_seconds)
            continue
        if status["state"] == STATE_RUNNING:
            await asyncio.sleep(poll_seconds)
            continue
        exit_code = status["exit_code"]
        break

    if exit_code is not None:
        # Before the container goes, not after: removing it destroys the
        # only copy of what the run was doing, and a run whose end needs
        # explaining is exactly the one whose logs have been deleted.
        archive_trainer_logs(output_name, entry.get("run_id", ""))
        remove_trainer(output_name)
    # The kill marks the journal, not this entry: the watcher was armed at
    # launch and is holding the dict as it was then, minutes before anyone
    # decided to stop the run.
    journal = read_journal(path) or {}
    cancelled = bool(entry.get("cancelled") or journal.get("cancelled"))
    outcome = "succeeded" if exit_code == 0 else "failed"
    if cancelled:
        # An operator stopping a run is not the run breaking. The exit
        # code is 137 either way, so the intent has to be recorded when
        # it is formed rather than inferred from the corpse.
        outcome = "cancelled"
    if exit_code is None:
        # Container vanished before anyone read it — someone removed it by
        # hand, or an older launch used --rm. Say so rather than guessing.
        outcome = "ended, outcome unknown"
    if close_run is not None:
        await safe_close_run(close_run, entry.get("run_id", ""), outcome, exit_code)
    result = await restore(entry, path)
    if notify is not None:
        await safe_notify(
            notify,
            f"training run {output_name} {outcome}"
            + (f" (exit {exit_code})" if exit_code is not None else "")
            + (
                "; voice is back"
                if not result["failures"]
                else f"; RESTORE FAILED for {', '.join(result['failures'])}"
            ),
        )
    return {"outcome": outcome, "exit_code": exit_code, **result}


async def safe_notify(notify, message: str) -> None:
    """A failed notification must never take the restore down with it."""
    try:
        outcome = notify(message)
        if asyncio.iscoroutine(outcome):
            await outcome
    except Exception as exc:  # noqa: BLE001 - transport of any kind may fail
        log.warning("training.gpu.notify_failed error=%s", exc)


async def notify_owner(message: str) -> bool:
    """Tell the operator directly over Telegram.

    Deliberately not routed through the gateway: these messages are about
    the machine being in an unusual state, and the gateway is one of the
    things that may be down or restarting when that is true. The bot
    token and chat id are already in this container's environment.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("training.gpu.notify_unconfigured")
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("training.gpu.notify_failed error=%s", exc)
        return False
    return True


async def reconcile(notify=None, path: Path | None = None, close_run=None) -> dict[str, Any]:
    """Pick the watch back up after this process restarted.

    A journal is only ever present between a preflight and its restore, so
    finding one means a run was in flight when we died. A trainer still
    alive gets its watcher back; a trainer gone gets restored immediately.
    A machine with no journal restores nothing — containers the operator
    stopped themselves are none of our business.
    """
    entry = read_journal(path)
    if not entry:
        return {"action": "nothing-to-do"}
    try:
        status = trainer_status(entry.get("output_name", ""))
    except DockerUnreachable:
        asyncio.ensure_future(watch(entry, notify=notify, path=path, close_run=close_run))
        return {"action": "resumed-watch", "reason": "docker unreachable"}
    if status["state"] == STATE_RUNNING:
        asyncio.ensure_future(watch(entry, notify=notify, path=path, close_run=close_run))
        return {"action": "resumed-watch"}
    result = await watch(entry, notify=notify, poll_seconds=0, path=path, close_run=close_run)
    return {"action": "restored", **result}
