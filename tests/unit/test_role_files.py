"""A mind given a role gets that role's hooks and skills by itself, and says
so when it cannot (story 10.18.15).

Nothing here calls the installer. Each test calls what an adapter calls to
prepare a harness process, the same function a chat turn and a terminal pane
go through, and then looks at what the harness would be handed.

Every home is a folder the test makes under pytest's ``tmp_path`` and pytest
removes; no real mind's folder is read or written. Daniel allowed this for
this code on 2026-11-18, 7:19 AM CST, asked whether it may be tested against
a scratch mind folder the test makes and deletes: "that's another good
example of a way to use that method to test a mind. tell him to proceed with
that". The gateway's answer, the proxy's model listing and the notifier are
transports and are the only things stood in for.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import subprocess
import time
import tomllib
from collections import deque
from pathlib import Path

import pytest

import minds.harness.claude_cli as claude
import minds.harness.codex_cli as codex
import minds.harness.dsh_cli as dsh
from core import hive_logging
from core.hive_logging import recent_errors
from minds import skill_reference, skills_api
from minds.harness import role_files, role_guard

HARNESSES = ["claude", "codex", "dsh"]
SKILL = "scratch-skill"
SKILL_TEXT = "---\nname: scratch-skill\ndescription: a skill a role names\n---\nDo the thing.\n"


@pytest.fixture()
def mind(monkeypatch, tmp_path):
    """A scratch mind: three empty harness homes, and a repository whose
    tracked skills are one skill. The hooks are the repository's own."""
    project = tmp_path / "project"
    skill = project / "specs" / "skills" / SKILL
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_TEXT)
    homes = {h: tmp_path / "minds" / "probe" / f".{h}" for h in HARNESSES}
    for home in homes.values():
        home.mkdir(parents=True)
    monkeypatch.setenv("MIND_NAME", "probe")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(homes["claude"]))
    monkeypatch.setenv("CODEX_HOME", str(homes["codex"]))
    monkeypatch.setenv("DSH_HOME", str(homes["dsh"]))
    monkeypatch.delenv("DSH_HOOKS_CONFIG", raising=False)
    monkeypatch.setattr(skills_api, "PROJECT_DIR", project)
    monkeypatch.setattr(skill_reference, "PROJECT_DIR", project)
    monkeypatch.setattr(skill_reference, "proxy_catalog", lambda *a: (lambda h: None))
    monkeypatch.setattr(skill_reference, "telegram_notifier", lambda *a: (lambda text: None))
    monkeypatch.setattr(claude, "CONFIG_DIR", homes["claude"])
    monkeypatch.setattr(codex, "CODEX_HOME", homes["codex"])
    monkeypatch.setattr(dsh, "DSH_HOME", homes["dsh"])
    # What this process has already said, and its error feed, belong to
    # whichever test ran last: each test reads only its own mind's lines.
    monkeypatch.setattr(role_files, "_SAID", {})
    monkeypatch.setattr(hive_logging, "_ERRORS", deque(maxlen=100))
    return homes


OWN = {"claude": ("settings.json", json.dumps(
           {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "bash /own/my_own.sh"}]}]}})),
       "dsh": ("hooks.json", json.dumps(
           {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "bash /own/my_own.sh"}]}]}})),
       "codex": ("config.toml", '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
                                'command = "bash /own/my_own.sh"\n')}


def _given(monkeypatch, *, hooks, skills):
    """The mind is given a role on the console: this is what the gateway
    then answers when the mind asks what its roles grant."""
    grants = {"managed": True, "roles": ["general"], "hooks": hooks,
              "skills": skills, "models": [], "tools": []}
    monkeypatch.setattr(role_guard, "fetch", lambda *a, **k: json.loads(json.dumps(grants)))


HOOKS_FILE = {"claude": "settings.json", "codex": "config.toml", "dsh": "hooks.json"}


def _prepare(harness: str) -> dict[str, str]:
    """What each adapter calls before it starts a harness process; returns
    the environment it built for that process."""
    if harness == "dsh":
        return dsh._role_hooks_env({})
    _args, env = (claude if harness == "claude" else codex)._role_guard({})
    return env


def _said(harness: str) -> list[str]:
    """The lines in the mind's error feed about this harness."""
    return [error["message"] for error in recent_errors()["errors"]
            if f"({harness})" in error["message"]]


def _start(harness: str, homes: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Prepare a harness process the way its adapter does, and return the
    hook commands that process is handed, by hook name, with the environment
    the adapter built for it."""
    env = _prepare(harness)
    if harness == "dsh":
        handed = json.loads(Path(env["DSH_HOOKS_CONFIG"]).read_text())
    elif harness == "claude":
        path = homes["claude"] / "settings.json"
        handed = json.loads(path.read_text()) if path.exists() else {}
    else:
        path = homes["codex"] / "config.toml"
        handed = tomllib.loads(path.read_text()) if path.exists() else {}
    commands: dict[str, str] = {}
    for groups in handed.get("hooks", {}).values():
        for group in groups:
            for item in group["hooks"]:
                words = shlex.split(item["command"])
                # dsh is handed each hook behind the role gate, which names it.
                name = (words[words.index("gate") + 1] if "gate" in words
                        else role_guard.hook_name(item["command"]))
                commands[name] = item["command"]
    return commands, env


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_mind_given_a_role_starts_its_next_conversation_with_that_roles_hooks_and_skills(
        monkeypatch, mind, harness):
    # A mind that already runs a hook of its own, as every real one does.
    name, text = OWN[harness]
    (mind[harness] / name).write_text(text)
    _given(monkeypatch, hooks=["surface_inject", "time_inject"], skills=[SKILL])

    commands, env = _start(harness, mind)

    # What it is handed runs: started as the harness starts it, each granted
    # hook says its piece: the surface it was told, and the time.
    said = {}
    for hook in ("surface_inject", "time_inject"):
        said[hook] = subprocess.run(
            commands[hook], shell=True, input="{}", text=True, capture_output=True, timeout=30,
            env={"PATH": os.environ["PATH"], **env, "COMMS_URL": "",
                 "HIVE_SURFACE": "telegram", "HIVE_MIND_TZ": "America/Chicago"}).stdout
    assert "<message-surface>telegram</message-surface>" in said["surface_inject"]
    assert "Current date and time" in said["time_inject"]
    assert "Do the thing." in (mind[harness] / "skills" / SKILL / "SKILL.md").read_text()


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_granted_hook_or_skill_that_cannot_be_put_in_place_is_named_in_the_error_feed(
        monkeypatch, mind, harness):
    hook, skill = f"untracked_hook_{harness}", f"untracked-skill-{harness}"
    # rotation_check is one of the four hooks each harness's own installer
    # puts in place; nothing has put it on this mind.
    _given(monkeypatch, hooks=["surface_inject", hook, "rotation_check"], skills=[skill])

    commands, _env = _start(harness, mind)

    said = _said(harness)
    assert any(f"hook {hook} is granted by a role, but the repository does not track it"
               in line for line in said)
    assert any("hook rotation_check is granted by a role and is not installed from here"
               in line for line in said)
    assert any(f"skill {skill} is granted by a role and is not there to use" in line
               for line in said)
    # And the conversation starts with what could be put in place.
    assert "surface_inject" in commands


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_tracked_hook_whose_files_cannot_be_written_is_named_in_the_error_feed(
        monkeypatch, mind, harness):
    # Where the installed scripts go is taken by a file, so none can be put there.
    (mind[harness] / "role-hooks").write_text("in the way\n")
    _given(monkeypatch, hooks=[f"untracked_{harness}", "skill_render_check"], skills=[])

    _start(harness, mind)

    assert any("skill_render_check" in line and "not installed" in line
               for line in _said(harness))


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_tracked_hook_found_registered_twice_is_named_in_the_error_feed(
        monkeypatch, mind, harness):
    _given(monkeypatch, hooks=["time_inject"], skills=[])
    _prepare(harness)
    # Somebody's edit leaves the installed hook registered a second time.
    hooks_file = mind[harness] / HOOKS_FILE[harness]
    if harness == "codex":
        text = hooks_file.read_text()
        hooks_file.write_text(text + "\n" + text[text.index("[[hooks.UserPromptSubmit]]"):])
    else:
        data = json.loads(hooks_file.read_text())
        data["hooks"]["UserPromptSubmit"] *= 2
        hooks_file.write_text(json.dumps(data, indent=2))

    _prepare(harness)

    assert any("hook time_inject is granted by a role and was not installed" in line
               and "more than once" in line for line in _said(harness))


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_hooks_file_that_cannot_be_read_is_named_in_the_error_feed(
        monkeypatch, mind, harness):
    hooks_file = mind[harness] / HOOKS_FILE[harness]
    hooks_file.write_text("{ this is neither JSON nor TOML")
    _given(monkeypatch, hooks=["prose_reminder"], skills=[])

    # dsh and Codex are not started on a hooks file they cannot read; that
    # refusal is the role guard's own and is said either way.
    with contextlib.suppress(role_guard.NotStarted):
        _prepare(harness)

    assert any("prose_reminder" in line and "not installed" in line and str(hooks_file) in line
               for line in _said(harness))


@pytest.mark.parametrize("harness", HARNESSES)
def test_an_installed_hook_the_mind_replaced_with_a_link_of_its_own_is_named_in_the_error_feed(
        monkeypatch, mind, tmp_path, harness):
    _given(monkeypatch, hooks=["time_inject"], skills=[])
    _prepare(harness)
    own = tmp_path / "the minds own clock.sh"
    own.write_text("#!/usr/bin/env bash\necho mine\n")
    installed = mind[harness] / "role-hooks" / "time_inject.sh"
    installed.unlink()
    installed.symlink_to(own)

    _prepare(harness)

    assert any("time_inject.sh under this mind's role-hooks folder is a link" in line
               for line in _said(harness))


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_granted_skill_whose_name_no_harness_would_load_is_named_in_the_error_feed(
        monkeypatch, mind, harness):
    # A folder inside a skill folder: there is a SKILL.md at the end of the
    # name, and no harness loads a skill from there.
    nested = mind[harness] / "skills" / "group" / "inner"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(SKILL_TEXT)
    _given(monkeypatch, hooks=[], skills=["group/inner"])

    _prepare(harness)

    assert any("skill group/inner is granted by a role" in line for line in _said(harness))


@pytest.mark.parametrize("harness", HARNESSES)
def test_grants_that_are_not_lists_of_names_are_named_in_the_error_feed(
        monkeypatch, mind, harness):
    # A gateway fault: one name where a list of names belongs.
    _given(monkeypatch, hooks="time_inject", skills=[])

    commands, _env = _start(harness, mind)

    assert any("the hooks this mind's roles grant came from the gateway as str" in line
               for line in _said(harness))
    # Not taken letter by letter either: nothing was registered.
    assert "time_inject" not in commands


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_conversation_that_starts_before_its_files_are_in_place_says_so(
        monkeypatch, mind, harness):
    """The install waits on a lock another process holds; the spawn does not."""
    _given(monkeypatch, hooks=["time_inject"], skills=[])
    hooks_file = mind[harness] / HOOKS_FILE[harness]
    with open(hooks_file.parent / f".{hooks_file.name}.role-hooks.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            # A terminal pane's timeout, shortened: the wait for files is a
            # third of it.
            began = time.monotonic()
            if harness == "dsh":
                dsh._role_hooks_env({}, timeout=0.3)
            else:
                (claude if harness == "claude" else codex)._role_guard({}, timeout=0.3)
            waited = time.monotonic() - began
            said = _said(harness)
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
    for worker, _began in list(role_files._RUNNING.values()):
        worker.join(10)

    assert any("started without waiting" in line for line in said)
    # A third of the caller's timeout, not the longest wait there is.
    assert waited < role_files.DEADLINE / 2
    # And what was waiting is in place once the lock is let go.
    assert (mind[harness] / "role-hooks" / "time_inject.sh").is_file()


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_granted_hook_registered_to_run_a_script_that_is_not_there_is_named_in_the_error_feed(
        monkeypatch, mind, tmp_path, harness):
    # The mind registers the hook itself, at a script that has since gone.
    gone = tmp_path / "gone" / "time_inject.sh"
    name, text = OWN[harness]
    (mind[harness] / name).write_text(
        text.replace("Stop", "UserPromptSubmit").replace("/own/my_own.sh", str(gone)))
    _given(monkeypatch, hooks=["time_inject"], skills=[])

    _prepare(harness)

    assert any(f"hook time_inject is granted by a role and is registered to run {gone}"
               in line for line in _said(harness))


def test_a_dsh_mind_whose_hooks_file_is_named_by_its_environment_gets_its_hooks_there(
        monkeypatch, mind, tmp_path):
    """As Stash's is: the file dsh is handed is made from the one the
    environment names, so that is where a granted hook has to arrive."""
    named = tmp_path / "minds" / "probe" / "hooks" / "hooks.json"
    named.parent.mkdir()
    named.write_text(OWN["dsh"][1])
    monkeypatch.setenv("DSH_HOOKS_CONFIG", str(named))
    _given(monkeypatch, hooks=["time_inject"], skills=[])

    commands, env = _start("dsh", mind)

    told = subprocess.run(
        commands["time_inject"], shell=True, input="{}", text=True, capture_output=True,
        timeout=30, env={"PATH": os.environ["PATH"], **env, "COMMS_URL": "",
                         "HIVE_MIND_TZ": "America/Chicago"}).stdout
    assert "Current date and time" in told
