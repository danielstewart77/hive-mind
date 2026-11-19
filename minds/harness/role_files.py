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
already stops a hook no role grants, and a skill the mind has is the mind's
to run whatever its roles list. What it does keep current is
what the installer itself put under ``<home>/role-hooks`` and registered:
that directory is the repository's, and a change there is how a hook changed
in the repository reaches the mind (story 10.18.12).

**It never stops a spawn, and never holds one for long.** The work runs on a
thread of its own against a deadline (a third of the caller's own timeout
and never more than `DEADLINE`: about three seconds for a chat turn, one for
a terminal pane, which is prepared on the event loop); whatever is not in place when it passes, or could not be
put in place at all, is logged as an error, which is what the mind's error
feed (`GET /diagnostics/errors`) carries to the console. The conversation
starts with what the mind has. One pass runs per harness home at a time: a
spawn that finds one still running waits on that one rather than starting
another, and a pass that outlives its spawn still says what it found.
"""

from __future__ import annotations

import logging
import math
import os
import shlex
import stat
import sys
import threading
import time
from pathlib import Path

from core.hive_logging import log_event
from minds.harness import role_guard, role_hooks

#: The longest a spawn waits for its role's files. An install with nothing
#: to do takes a few milliseconds; this is for a lock another process holds
#: or a skill install that has to ask the proxy for its catalogue.
DEADLINE = 5.0

#: The error feed keeps an hour. A problem that is still there is said again
#: inside that, so it does not fall out of the feed while it stands.
REPEAT_AFTER = 1800.0

#: The one problem a finished pass does not forget: a pass that was late for
#: its spawn and then ran clean would otherwise say so again at every spawn.
LATE = ("all", "deadline")

#: An earlier pass that never ended, said in its own words.
STUCK = ("all", "stuck")

#: When each standing problem was last logged, on the clock that never
#: steps back, by harness, home and a key
#: that does not change from turn to turn. A finished pass keeps only what
#: it saw for its own home, so a problem that is fixed and comes back is
#: said again, one home's clean pass says nothing about another's, and the
#: table cannot grow past what one mind's grants can name.
_SAID: dict[tuple, float] = {}
_SAID_LOCK = threading.Lock()

#: The pass running for each (harness, home) and when it began, so there is
#: never a second.
_RUNNING: dict[tuple[str, str], tuple[threading.Thread, float]] = {}
_RUNNING_LOCK = threading.Lock()

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


def _registered(harness: str, hooks_file: Path) -> dict[str, list[str]] | None:
    """The commands this mind's hooks file registers, by hook name; None
    when the file is there and cannot be read as its format."""
    try:
        text = hooks_file.read_text() if hooks_file.exists() else ""
        entries = role_hooks._open(harness, text).entries()
    except (OSError, UnicodeDecodeError, role_hooks.HooksFileError):
        return None
    found: dict[str, list[str]] = {}
    for _event, command, _timeout in entries:
        name = role_guard.hook_name(command)
        found.setdefault(name, []).append(command)
        if name == "rotation_hooks":
            # Claude's and Codex's rotation hooks are one generated script
            # taking the hook as its action.
            try:
                words = shlex.split(command)
            except ValueError:
                words = command.split()
            for action in ("check", "fire"):
                if action in words:
                    found.setdefault(f"rotation_{action}", []).append(command)
    return found


def _missing_script(command: str) -> str | None:
    """The script a registered command names by its full path, when that
    path is not there; None when it is, or the command names none that way."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for word in words:
        if word.endswith(role_guard.SCRIPT_SUFFIXES):
            # Only a plain full path can be judged from here: the shell
            # fills in a variable, and a relative path depends on where the
            # harness runs.
            if not os.path.isabs(word) or any(mark in word for mark in "$`*?[{~"):
                return None
            try:
                there = stat.S_ISREG(os.stat(word).st_mode)
            except (FileNotFoundError, NotADirectoryError):
                there = False
            except (OSError, ValueError):
                # A folder that cannot be looked into, or a path no file
                # could have, says nothing either way.
                return None
            return None if there else word
    return None


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
    for name in report.get("links_kept", []):
        problems.append((("link", name),
                         f"{name} under this mind's role-hooks folder is a link the mind "
                         "made; it was left as it is, and the repository's file was not "
                         "put there"))
    if report.get("error"):
        problems.append((("hooks", "file"),
                         f"hooks {', '.join(names)} are granted by a role and were not "
                         f"installed: {report['error']}"))
    if not report["hooks"]:
        return report["hooks"], problems
    registered = _registered(harness, Path(report["hooks_file"]))
    if registered is None:
        if not report.get("error"):
            problems.append((("hooks", "file"),
                             f"hooks {', '.join(names)} are granted by a role, and this "
                             f"mind's hooks file {report['hooks_file']} cannot be read to "
                             "see which of them it runs"))
        return report["hooks"], problems
    for name, outcome in report["hooks"].items():
        status = outcome.get("status")
        if (status == "failed" and report.get("error")
                and outcome.get("reason") in (None, report["error"])):
            continue  # said once above, for every hook it cost
        if status in ("failed", "registered_twice"):
            reason = outcome.get("reason") or status.replace("_", " ")
            problems.append((("hook", name),
                             f"hook {name} is granted by a role and was not installed: {reason}"))
        elif name not in registered and status == "unknown":
            problems.append((("hook", name),
                             f"hook {name} is granted by a role, but the repository does "
                             "not track it and this mind registers no hook of that name"))
        elif name not in registered and status == "baseline":
            # One of the four each harness's own installer puts in place.
            # Only dsh has a rotation fire hook that is a file of its own.
            if name != "rotation_fire" or harness == "dsh":
                problems.append((("hook", name),
                                 f"hook {name} is granted by a role and is not installed "
                                 "from here, and this mind registers no hook of that name"))
        else:
            # Registered, by this installer or by the mind: the grant is
            # still not met when the script it runs is not there.
            for command in registered.get(name, []):
                gone = _missing_script(command)
                if gone:
                    problems.append((("hook", name),
                                     f"hook {name} is granted by a role and is registered "
                                     f"to run {gone}, which is not there"))
                    break
    return report["hooks"], problems


def _skills(harness: str, names: list[str]) -> tuple[dict, list[Problem]]:
    # Imported here: it brings the web framework with it, and this module is
    # also imported where only the hooks half is wanted.
    from minds import skills_api

    done: dict[str, str] = {}
    problems: list[Problem] = []
    for name in names:
        try:
            skills_api._validate(name)
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
          python: str, out: dict, log: logging.Logger | None) -> None:
    problems: list[Problem] = []
    try:
        hooks, shape = _names(grants, "hooks")
        problems += shape
        skills, shape = _names(grants, "skills")
        problems += shape
        try:
            out["hooks"], found = _hooks(harness, home, hooks, hooks_file, python)
            problems += found
        except Exception as exc:  # noqa: BLE001 - reported, never raised into a spawn
            problems.append((("hooks", "raised"),
                             f"hooks {', '.join(hooks)} are granted by a role and were not "
                             f"installed: {exc}"))
        try:
            out["skills"], found = _skills(harness, skills)
            problems += found
        except Exception as exc:  # noqa: BLE001
            problems.append((("skills", "raised"),
                             f"skills {', '.join(skills)} are granted by a role and were not "
                             f"installed: {exc}"))
    except BaseException as exc:  # noqa: BLE001 - a thread that dies says why
        problems.append((("all", "raised"),
                         f"the hooks and skills this mind's roles grant were not put in "
                         f"place: {type(exc).__name__}: {exc}"))
    out["problems"] = [text for _key, text in problems]
    # Said from here, so a pass that outlived its spawn still names what it
    # found.
    _say(log, harness, home, problems, time.monotonic(), finished=True)


def _say(log: logging.Logger | None, harness: str, home: Path, problems: list[Problem],
         now: float, *, finished: bool) -> None:
    if log is None:
        return  # nothing is said, so nothing is remembered as said
    whose = (harness, str(home))
    keyed = [((*whose, *key), text) for key, text in problems]
    with _SAID_LOCK:
        due = [(key, text) for key, text in keyed
               if now - _SAID.get(key, float("-inf")) >= REPEAT_AFTER]
        if finished:
            # Only a pass that ran to its end knows what no longer stands.
            seen = {key for key, _text in keyed}
            for key in [k for k in _SAID
                        if k[:2] == whose and k not in seen and k[2:] != LATE]:
                del _SAID[key]
        _SAID.update({key: now for key, _text in due})
    for _key, text in due:
        # The sentence is the message: the error feed carries a record's
        # message and nothing of its fields.
        log_event(log, f"a role's file did not reach this mind ({harness}): {text}",
                  level=logging.ERROR, problem=text, harness=harness)


def provision(harness: str, home: Path, grants: dict, *, hooks_file: Path | None = None,
              python: str | None = None, log: logging.Logger | None = None,
              deadline: float = DEADLINE) -> dict:
    """Put the hooks and skills these grants name onto this mind.

    Returns what was done per hook and per skill, and ``problems``: one
    sentence for each granted file that is still not in place. Never raises,
    and returns within *deadline* seconds, which is held between a twentieth
    of a second and `DEADLINE`. A mind that is not under role management is
    left exactly as it is.
    """
    managed = isinstance(grants, dict) and grants.get("managed") is True
    result: dict = {"managed": managed, "hooks": {}, "skills": {}, "problems": []}
    if not managed:
        return result
    try:
        # By its spelling alone: nothing on this thread looks at the disc,
        # so a mount that hangs holds the pass and not the spawn.
        home = Path(os.path.abspath(home))
        wait = float(deadline)
        wait = DEADLINE if math.isnan(wait) else min(DEADLINE, max(0.05, wait))
        key = (harness, str(home))
        until = time.monotonic() + wait
        out: dict = {"hooks": {}, "skills": {}, "problems": []}
        mine = None
        stuck = 0.0
        while mine is None:
            with _RUNNING_LOCK:
                other, began = _RUNNING.get(key, (None, 0.0))
                if other is None or not other.is_alive():
                    mine = threading.Thread(
                        target=_work, name="role-files", daemon=True,
                        args=(harness, home, grants, hooks_file,
                              python or sys.executable or "/usr/bin/python3", out, log))
                    _RUNNING[key] = (mine, time.monotonic())
                    mine.start()
                    break
            # An earlier spawn's pass is still at work on this home. One that
            # has already run past the longest wait is stuck, and is not
            # waited on again at every spawn.
            waited_on = time.monotonic() - began <= DEADLINE
            if waited_on:
                other.join(max(0.0, until - time.monotonic()))
            if other.is_alive():
                if not waited_on:
                    stuck = time.monotonic() - began
                break
        if mine is not None:
            mine.join(max(0.0, until - time.monotonic()))
        if stuck:
            text = (f"an earlier install of this mind's hooks and skills has been running "
                    f"for {stuck:.0f} seconds and has not ended; nothing more is installed "
                    "for it until that ends or its server is restarted")
            _say(log, harness, home, [(STUCK, text)], time.monotonic(), finished=False)
            result["problems"] = [text]
            return result
        if mine is None or mine.is_alive():
            # Left to finish behind the conversation: what it installs is
            # there for the next spawn, and it says itself what it finds.
            late = ("the hooks and skills this mind's roles grant were not all in place "
                    f"after {wait:g} seconds, and the conversation started without waiting")
            _say(log, harness, home, [(LATE, late)], time.monotonic(), finished=False)
            result["problems"] = [late]
            return result
        result.update(hooks=dict(out["hooks"]), skills=dict(out["skills"]),
                      problems=list(out["problems"]))
    except Exception as exc:  # noqa: BLE001 - a thread that cannot start, say
        text = (f"the hooks and skills this mind's roles grant were not put in place: "
                f"{type(exc).__name__}: {exc}")
        result["problems"] = [text]
        _say(log, harness, Path(str(home)), [(("all", "raised"), text)], time.monotonic(),
             finished=False)
    return result
