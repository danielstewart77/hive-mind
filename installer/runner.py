"""Running the steps in order, recording each, one run at a time.

`state/run.json` holds every step's status (pending, running, done, failed,
waiting), when it ran and why it failed, so the page reads it and a page
reopened after an SSH drop picks up where the run is.

Every run repeats every step, in order. Each step is safe to repeat — it
checks before it changes, and keeps what is there — so a step that failed is
retried after the earlier ones have re-confirmed their work, a fix pushed to
the repositories takes effect on the next run, and a step found `running`
(the process died under it) is simply run again.

A lock file keeps two runs off one install. Each step has a deadline; every
command it runs is bounded by what is left of it, and a step that passes it
is failed with the step's name, even if a thread is still blocked inside it.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from installer.paths import Home, write_private
from installer.shell import Shell, StepFailed

PENDING, RUNNING, DONE, FAILED, WAITING = "pending", "running", "done", "failed", "waiting"


class NeedInput(Exception):
    """A step that needs the operator: it pauses the run with a question."""

    def __init__(self, key: str, label: str, choices: list[str], reason: str = ""):
        super().__init__(label)
        self.key, self.label, self.choices, self.reason = key, label, choices, reason


@dataclass(frozen=True)
class Step:
    name: str
    label: str
    fn: Callable
    deadline: float = 600


class Locked(RuntimeError):
    pass


class RunLock:
    def __init__(self, home: Home):
        self.home = home
        self._fh = None

    def __enter__(self):
        self.home.state.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.home.lock, "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            raise Locked("another install run holds this install folder's lock") from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def read_state(home: Home) -> dict:
    try:
        return json.loads(home.run_json.read_text())
    except (FileNotFoundError, ValueError):
        return {"steps": [], "finished": False}


class Runner:
    def __init__(self, home: Home, steps: list[Step], ctx_factory: Callable):
        self.home = home
        self.steps = steps
        self.ctx_factory = ctx_factory
        self._mutex = threading.Lock()
        self.answered = threading.Event()
        self.state = read_state(home)

    # ---- the record -------------------------------------------------------
    def _save(self) -> None:
        write_private(self.home.run_json, json.dumps(self.state, indent=2))

    def _entry(self, step: Step) -> dict:
        for e in self.state["steps"]:
            if e["name"] == step.name:
                return e
        e = {"name": step.name, "label": step.label, "status": PENDING}
        self.state["steps"].append(e)
        return e

    def _set(self, step: Step, **fields) -> None:
        with self._mutex:
            e = self._entry(step)
            e.update(fields)
            self._save()

    def snapshot(self) -> dict:
        with self._mutex:
            return json.loads(json.dumps(self.state))

    # ---- the run ----------------------------------------------------------
    def run(self) -> bool:
        """Every step in order; stops at the first that fails. True when all
        are done."""
        with RunLock(self.home):
            with self._mutex:
                known = {e["name"]: e for e in self.state.get("steps", [])}
                self.state = {
                    "run_started": time.time(), "finished": False, "failed_step": None,
                    "steps": [{"name": s.name, "label": s.label,
                               "status": known.get(s.name, {}).get("status", PENDING),
                               "previous": known.get(s.name, {}).get("status")}
                              for s in self.steps],
                    "outputs": self.state.get("outputs", {}),
                }
                # This run repeats every step; what the last run left is
                # kept only as `previous`, for the page to show.
                for e in self.state["steps"]:
                    e["status"] = PENDING
                self._save()
            for step in self.steps:
                if not self._run_one(step):
                    with self._mutex:
                        self.state["failed_step"] = step.name
                        self._save()
                    return False
            with self._mutex:
                self.state["finished"] = True
                self.state["finished_at"] = time.time()
                self._save()
            return True

    def _run_one(self, step: Step) -> bool:
        while True:
            started = time.monotonic()
            self._set(step, status=RUNNING, started=time.time(), ended=None, error=None,
                      question=None)
            result: dict = {}
            deadline = started + step.deadline

            def target():
                try:
                    ctx = self.ctx_factory(Shell(self.home.log, deadline))
                    result["detail"] = step.fn(ctx)
                except BaseException as exc:  # noqa: BLE001 - recorded, not swallowed
                    result["exc"] = exc
                    result["trace"] = traceback.format_exc()

            worker = threading.Thread(target=target, name=f"step-{step.name}", daemon=True)
            worker.start()
            worker.join(step.deadline + 5)
            if worker.is_alive():
                self._set(step, status=FAILED, ended=time.time(),
                          error=f"{step.label} took longer than {int(step.deadline)}s")
                return False
            exc = result.get("exc")
            if isinstance(exc, NeedInput):
                self.answered.clear()
                self._set(step, status=WAITING, question={
                    "key": exc.key, "label": exc.label, "choices": exc.choices,
                    "reason": exc.reason})
                self.answered.wait()
                continue
            if exc is not None:
                message = str(exc) if isinstance(exc, StepFailed) else \
                    f"{type(exc).__name__}: {exc}"
                Shell(self.home.log).log(result.get("trace", ""))
                self._set(step, status=FAILED, ended=time.time(), error=message)
                return False
            detail = result.get("detail")
            self._set(step, status=DONE, ended=time.time(),
                      detail=detail if isinstance(detail, (str, dict, list)) else None)
            return True

    def record_output(self, key: str, value) -> None:
        with self._mutex:
            self.state.setdefault("outputs", {})[key] = value
            self._save()
