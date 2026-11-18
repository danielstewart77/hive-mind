"""The files a role names, put on the mind that holds it (story 10.18.15).

A role grants four things. A model and the built-in tools are names the
gateway checks; hooks and skills are files, and a mind granted one it has no
file for used to skip it without a word. `provision` closes that: every
adapter calls it with the grants it has just fetched for a spawn, before the
harness is handed its hooks, so a mind given a role on the console has that
role's hooks and skills from its next conversation with nobody running
anything.

It runs inside the mind, from the mind's own checkout, which is why it holds
for a container in this stack and a mind on another machine alike: no other
process can see a mind's harness home.

**It only ever adds.** A hook the mind registered by hand is kept
(`role_hooks.install` without ``replace``), a skill the mind already has is
not touched, and nothing is removed when a role is taken away: the guard
already stops a hook or skill no role grants.

**It never stops a spawn.** Whatever cannot be put in place is returned and
logged as an error, which is what the mind's error feed
(`GET /diagnostics/errors`) carries to the console; the conversation starts
with what the mind has.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from core.hive_logging import log_event
from minds.harness import role_guard, role_hooks

#: Problems already logged by this process. A spawn happens every turn, and
#: one missing file is one line in the error feed, not one per turn.
_SAID: set[str] = set()
_SAID_LOCK = threading.Lock()

#: Hook statuses that mean the granted hook is not running as tracked and
#: nothing the mind itself keeps explains it.
_HOOK_FAILURES = ("failed", "registered_twice")


def _registered_names(harness: str, hooks_file: Path) -> set[str]:
    """The names of the hooks this mind's hooks file registers."""
    try:
        text = hooks_file.read_text() if hooks_file.exists() else ""
        entries = role_hooks._open(harness, text).entries()
    except (OSError, UnicodeDecodeError, role_hooks.HooksFileError):
        return set()
    return {role_guard.hook_name(command) for _event, command, _timeout in entries}


def _hooks(harness: str, home: Path, names: list[str], hooks_file: Path | None,
           python: str) -> tuple[dict, list[str]]:
    report = role_hooks.install(harness, home, names, hooks_file=hooks_file, python=python)
    problems: list[str] = []
    if report.get("error"):
        problems.append(f"the hooks its roles grant could not be installed: {report['error']}")
    unknown = [name for name, outcome in report["hooks"].items()
               if outcome.get("status") == "unknown"]
    if unknown:
        registered = _registered_names(harness, Path(report["hooks_file"]))
        for name in unknown:
            if name not in registered:
                problems.append(
                    f"hook {name} is granted by a role, but the repository does not "
                    "track it and this mind registers no hook of that name")
    for name, outcome in report["hooks"].items():
        if outcome.get("status") in _HOOK_FAILURES:
            reason = outcome.get("reason") or outcome["status"].replace("_", " ")
            problems.append(f"hook {name} is granted by a role and was not installed: {reason}")
    return report["hooks"], problems


def _skills(harness: str, names: list[str]) -> tuple[dict, list[str]]:
    # Imported here: it brings the web framework with it, and this module is
    # also imported where only the hooks half is wanted.
    from minds import skills_api

    done: dict[str, str] = {}
    problems: list[str] = []
    for name in names:
        try:
            state = skills_api._pair(skills_api.harness_directory(harness), name).state
            kept = ((skills_api.installed_root(harness) / name).exists()
                    or (skills_api.reference_root() / name).exists())
            if state == skills_api.STATE_NOT_INSTALLED and not kept:
                # Only a skill the mind has no copy of at all: installing
                # replaces the harness's copy whatever it held.
                skills_api.install_skill(harness, name)
                state = "installed"
            elif state == skills_api.STATE_LOCAL_ONLY and not kept:
                problems.append(
                    f"skill {name} is granted by a role, but the repository does not "
                    "track it and this mind has no copy of it")
                state = "missing"
            done[name] = state
        except Exception as exc:  # noqa: BLE001 - one skill never costs the rest, or the spawn
            done[name] = "failed"
            problems.append(f"skill {name} is granted by a role and was not installed: {exc}")
    return done, problems


def provision(harness: str, home: Path, grants: dict, *, hooks_file: Path | None = None,
              python: str | None = None, log: logging.Logger | None = None) -> dict:
    """Put the hooks and skills these grants name onto this mind.

    Returns what was done per hook and per skill, and ``problems``: one
    sentence for each granted file that is still not in place. Never raises.
    A mind that is not under role management is left exactly as it is.
    """
    result: dict = {"managed": bool(grants.get("managed")), "hooks": {}, "skills": {},
                    "problems": []}
    if not result["managed"]:
        return result
    hooks = [str(name) for name in grants.get("hooks") or []]
    skills = [str(name) for name in grants.get("skills") or []]
    try:
        result["hooks"], problems = _hooks(
            harness, Path(home), hooks, hooks_file, python or sys.executable or "/usr/bin/python3")
        result["problems"] += problems
    except Exception as exc:  # noqa: BLE001 - reported, never raised into a spawn
        result["problems"].append(f"the hooks its roles grant could not be installed: {exc}")
    try:
        result["skills"], problems = _skills(harness, skills)
        result["problems"] += problems
    except Exception as exc:  # noqa: BLE001
        result["problems"].append(f"the skills its roles grant could not be installed: {exc}")
    if log is not None:
        for problem in result["problems"]:
            with _SAID_LOCK:
                new = problem not in _SAID
                _SAID.add(problem)
            if new:
                # The sentence is the message: the error feed carries a
                # record's message and nothing of its fields.
                log_event(log, f"a role's file did not reach this mind: {problem}",
                          level=logging.ERROR, problem=problem)
    return result
