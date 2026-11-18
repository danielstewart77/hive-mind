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

import json
import os
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest

import minds.harness.claude_cli as claude
import minds.harness.codex_cli as codex
import minds.harness.dsh_cli as dsh
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
    # What this process has already said belongs to whichever test ran last.
    monkeypatch.setattr(role_files, "_SAID", {})
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
    grants = {"managed": True, "roles": ["general"], "hooks": list(hooks),
              "skills": list(skills), "models": [], "tools": []}
    monkeypatch.setattr(role_guard, "fetch", lambda *a, **k: json.loads(json.dumps(grants)))


def _start(harness: str, homes: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Prepare a harness process the way its adapter does, and return the
    hook commands that process is handed, by hook name, with the environment
    the adapter built for it."""
    if harness == "dsh":
        env = dsh._role_hooks_env({})
        handed = json.loads(Path(env["DSH_HOOKS_CONFIG"]).read_text())
    elif harness == "claude":
        _args, env = claude._role_guard({})
        path = homes["claude"] / "settings.json"
        handed = json.loads(path.read_text()) if path.exists() else {}
    else:
        _args, env = codex._role_guard({})
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

    said = [error["message"] for error in recent_errors()["errors"]]
    assert any(f"hook {hook} is granted by a role" in line for line in said)
    assert any("hook rotation_check is granted by a role" in line for line in said)
    assert any(f"skill {skill} is granted by a role" in line for line in said)
    # And the conversation starts with what could be put in place.
    assert "surface_inject" in commands


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_tracked_hook_whose_files_cannot_be_written_is_named_in_the_error_feed(
        monkeypatch, mind, harness):
    # Where the installed scripts go is taken by a file, so none can be put there.
    (mind[harness] / "role-hooks").write_text("in the way\n")
    _given(monkeypatch, hooks=[f"untracked_{harness}", "skill_render_check"], skills=[])

    _start(harness, mind)

    said = [error["message"] for error in recent_errors()["errors"]]
    assert any("skill_render_check" in line and "not installed" in line and f"({harness})" in line
               for line in said)
