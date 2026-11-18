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
(`role_hooks.install` without ``replace``), a skill the mind has any copy of
is not touched, and nothing is removed when a role is taken away: the guard
already stops a hook or skill no role grants. What it does keep current is
what the installer itself put under ``<home>/role-hooks`` and registered:
that directory is the repository's, and a change there is how a hook changed
in the repository reaches the mind (story 10.18.12).

**It never stops a spawn, and never holds one for long.** The work runs
against a deadline (`DEADLINE`); whatever is not in place when it passes, or
could not be put in place at all, is returned and logged as an error, which
is what the mind's error feed (`GET /diagnostics/errors`) carries to the
console. The conversation starts with what the mind has.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

from core.hive_logging import log_event
from minds.harness import role_guard, role_hooks

#: How long a spawn waits for its role's files. An install with nothing to
#: do takes a few milliseconds; this is for a lock another process holds or
#: a skill install that has to ask the proxy for its catalogue.
DEADLINE = 5.0

#: The error feed keeps an hour. A problem that is still there is said again
#: inside that, so it does not fall out of the feed while it stands.
REPEAT_AFTER = 1800.0

#: When each standing problem was last logged, by a key that does not change
#: from turn to turn. Only problems seen on the latest pass are kept, so one
#: that is fixed and comes back is said again, and the table cannot grow
#: past what one mind's grants can name.
_SAID: dict[tuple[str, str], float] = {}
_SAID_LOCK = threading.Lock()

Problem = tuple[tuple[str, str], str]


def _names(grants: dict, key: str) -> tuple[list[str], list[Problem]]:
    """The names a grant list holds, and a problem when it is not a list of
    names: a string is not taken letter by letter, nor a mapping by its keys."""
    value = grants.get(key)
    if value is None:
        return [], []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        return [], [((key, "shape"),
                     f"the {key} this mind's roles grant came from the gateway as "
                     f"{type(value).__name__}, not a list of names; none were installed")]
    return list(dict.fromkeys(value)), []


def _registered_names(harness: str, hooks_file: Path) -> set[str]:
    """The names of the hooks this mind's hooks file registers."""
    try:
        text = hooks_file.read_text() if hooks_file.exists() else ""
        entries = role_hooks._open(harness, text).entries()
    except (OSError, UnicodeDecodeError, role_hooks.HooksFileError):
        return set()
    return {role_guard.hook_name(command) for _event, command, _timeout in entries}


def _hooks(harness: str, home: Path, names: list[str], hooks_file: Path | None,
           python: str) -> tuple[dict, list[Problem]]:
    if hooks_file is not None and hooks_file.name == "hooks.effective.json":
        # The file the role guard generates at every spawn. Registering into
        # it would add the hook again each turn and lose it at the next.
        return {}, [(("hooks", "generated"),
                     f"hooks {', '.join(names)} are granted by a role and were not installed: "
                     f"this mind's hooks file is set to {hooks_file}, which is generated at "
                     "every spawn; name the file it is generated from")]
    report = role_hooks.install(harness, home, names, hooks_file=hooks_file, python=python)
    problems: list[Problem] = []
    if report.get("error"):
        problems.append((("hooks", "file"),
                         f"hooks {', '.join(names)} are granted by a role and were not "
                         f"installed: {report['error']}"))
    registered: set[str] | None = None
    for name, outcome in report["hooks"].items():
        status = outcome.get("status")
        if status == "unknown":
            if registered is None:
                registered = _registered_names(harness, Path(report["hooks_file"]))
            if name not in registered:
                problems.append((("hook", name),
                                 f"hook {name} is granted by a role, but the repository does "
                                 "not track it and this mind registers no hook of that name"))
        elif status in ("failed", "registered_twice"):
            reason = outcome.get("reason") or status.replace("_", " ")
            problems.append((("hook", name),
                             f"hook {name} is granted by a role and was not installed: {reason}"))
    return report["hooks"], problems


def _skills(harness: str, names: list[str]) -> tuple[dict, list[Problem]]:
    # Imported here: it brings the web framework with it, and this module is
    # also imported where only the hooks half is wanted.
    from minds import skills_api

    done: dict[str, str] = {}
    problems: list[Problem] = []
    for name in names:
        try:
            h = skills_api.harness_directory(harness)
            copy = skills_api.installed_root(harness) / name
            state = skills_api._pair(h, name).state
            # Any copy at all, a dangling link included: installing replaces
            # the harness's copy whatever it held, and that is the mind's.
            kept = os.path.lexists(copy) or os.path.lexists(skills_api.reference_root() / name)
            if state == skills_api.STATE_NOT_INSTALLED and not kept:
                skills_api.install_skill(harness, name)
                state = "installed"
            done[name] = state
            # Whatever happened above, the grant is met only when the harness
            # has a skill of that name to load.
            if not (copy / skills_api.SKILL_FILE).is_file():
                done[name] = "missing"
                if state == skills_api.STATE_LOCAL_ONLY and not kept:
                    why = "the repository does not track it and this mind has no copy of it"
                elif kept:
                    why = (f"this mind keeps a copy of its own that the {h} harness cannot "
                           "load, which was left as it is")
                else:
                    why = f"it could not be read on this mind ({state})"
                problems.append((("skill", name),
                                 f"skill {name} is granted by a role and is not there to "
                                 f"use: {why}"))
        except Exception as exc:  # noqa: BLE001 - one skill never costs the rest, or the spawn
            done[name] = "failed"
            problems.append((("skill", name),
                             f"skill {name} is granted by a role and was not installed: {exc}"))
    return done, problems


def _work(harness: str, home: Path, grants: dict, hooks_file: Path | None,
          python: str, result: dict, problems: list[Problem]) -> None:
    hooks, shape = _names(grants, "hooks")
    problems += shape
    skills, shape = _names(grants, "skills")
    problems += shape
    try:
        result["hooks"], found = _hooks(harness, home, hooks, hooks_file, python)
        problems += found
    except Exception as exc:  # noqa: BLE001 - reported, never raised into a spawn
        problems.append((("hooks", "raised"),
                         f"hooks {', '.join(hooks)} are granted by a role and were not "
                         f"installed: {exc}"))
    try:
        result["skills"], found = _skills(harness, skills)
        problems += found
    except Exception as exc:  # noqa: BLE001
        problems.append((("skills", "raised"),
                         f"skills {', '.join(skills)} are granted by a role and were not "
                         f"installed: {exc}"))


def _say(log: logging.Logger | None, problems: list[Problem], now: float) -> None:
    with _SAID_LOCK:
        due = [(key, text) for key, text in problems
               if now - _SAID.get(key, float("-inf")) >= REPEAT_AFTER]
        standing = {key: _SAID.get(key, now) for key, _text in problems}
        standing.update({key: now for key, _text in due})
        _SAID.clear()
        _SAID.update(standing)
    if log is None:
        return
    for _key, text in due:
        # The sentence is the message: the error feed carries a record's
        # message and nothing of its fields.
        log_event(log, f"a role's file did not reach this mind: {text}",
                  level=logging.ERROR, problem=text)


def provision(harness: str, home: Path, grants: dict, *, hooks_file: Path | None = None,
              python: str | None = None, log: logging.Logger | None = None,
              deadline: float = DEADLINE) -> dict:
    """Put the hooks and skills these grants name onto this mind.

    Returns what was done per hook and per skill, and ``problems``: one
    sentence for each granted file that is still not in place. Never raises,
    and returns within *deadline* seconds. A mind that is not under role
    management is left exactly as it is.
    """
    managed = isinstance(grants, dict) and grants.get("managed") is True
    result: dict = {"managed": managed, "hooks": {}, "skills": {}, "problems": []}
    if not managed:
        return result
    problems: list[Problem] = []
    worker = threading.Thread(
        target=_work, name="role-files", daemon=True,
        args=(harness, Path(home), grants, hooks_file,
              python or sys.executable or "/usr/bin/python3", result, problems))
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        # Left to finish behind the conversation: what it installs is there
        # for the next spawn, and this one is not held for it.
        problems = [(("all", "deadline"),
                     f"the hooks and skills this mind's roles grant were not all in place "
                     f"after {deadline:g} seconds, and the conversation started without waiting")]
    result["problems"] = [text for _key, text in problems]
    _say(log, problems, time.time())
    return result
