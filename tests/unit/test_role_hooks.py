"""The hooks a role names, installed into a mind's harness home.

Every home here is a folder the test makes under pytest's ``tmp_path`` and
pytest removes; no real mind's folder is read or written. Daniel allowed
this for the installer on 2026-11-17: its tests may run it against a
throwaway mind folder the test itself creates and deletes.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from minds.harness import role_guard, role_hooks

#: The hooks the General role names.
GENERAL = ["bg_snapshot", "prose_reminder", "skill_render_check", "skill_telemetry",
           "soul_proposal_inject", "surface_inject", "time_inject", "training_capture"]

HOME = {"claude": ".claude", "codex": ".codex", "dsh": ".dsh"}


def _home(tmp_path: Path, harness: str) -> Path:
    home = tmp_path / "minds" / "probe" / HOME[harness]
    home.mkdir(parents=True)
    return home


def _registered(harness: str, home: Path) -> list[tuple[str, str, int | None]]:
    """Every (event, command, timeout) in the home's hooks file, read with
    the format's own parser rather than the installer's."""
    path = role_hooks.hooks_file_for(harness, home)
    data = (tomllib.loads(path.read_text()) if harness == "codex"
            else json.loads(path.read_text()))
    return [(event, item["command"], item.get("timeout"))
            for event, groups in data["hooks"].items()
            for group in groups for item in group["hooks"]]


def _run(command: str, payload: dict | None = None, **environ: str) -> str:
    """Run a registered command the way a harness does: through a shell,
    with its payload on stdin, in an environment that names nothing."""
    done = subprocess.run(command, shell=True, input=json.dumps(payload or {}), text=True,
                          capture_output=True, timeout=30,
                          env={"PATH": os.environ["PATH"], **environ})
    return done.stdout


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_a_mind_ends_with_every_named_hook_installed_and_registered(tmp_path, harness):
    home = _home(tmp_path, harness)

    role_hooks.install(harness, home, GENERAL, python=sys.executable)

    registered = _registered(harness, home)
    by_name: dict[str, list[str]] = {}
    for _event, command, _timeout in registered:
        by_name.setdefault(role_guard.hook_name(command), []).append(command)
    assert sorted(by_name) == sorted(GENERAL)
    for commands in by_name.values():
        for command in commands:
            script = next(Path(word) for word in shlex.split(command)
                          if word.endswith((".sh", ".py")))
            assert script.is_file(), f"{command} names a script that is not there"
    # And what is registered runs: the surface hook, as its harness would
    # start it, says the surface it was handed.
    spoken = json.loads(_run(by_name["surface_inject"][0], HIVE_SURFACE="telegram"))
    assert "<message-surface>telegram</message-surface>" in (
        spoken["hookSpecificOutput"]["additionalContext"])
    background = {event for event, command, _ in registered
                  if role_guard.hook_name(command) == "bg_snapshot"}
    assert background == {"Stop", "SubagentStop"}


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_an_installed_hook_has_the_files_and_arguments_it_runs_with(tmp_path, harness):
    """Registered is not enough: each hook that needs a file beside it, or
    is told its harness, does its work when its harness starts it."""
    home = _home(tmp_path, harness)
    state = tmp_path / "state"
    role_hooks.install(harness, home, GENERAL, python=sys.executable)
    command = {role_guard.hook_name(entry[1]): entry[1]
               for entry in _registered(harness, home)}

    # The prose reminder names the operator through a file beside it.
    spoken = json.loads(_run(command["prose_reminder"], HIVE_SURFACE="telegram"))
    assert "read aloud by TTS to the operator," in (
        spoken["hookSpecificOutput"]["additionalContext"])

    # The background snapshot records through a module beside it.
    _run(command["bg_snapshot"],
         {"session_id": "abc", "background_tasks": [{"id": "1", "status": "running"}]},
         HIVE_SURFACE="terminal", AUTO_REMEMBER_LOG_DIR=str(state))
    recorded = json.loads((state / "bgtasks-abc.json").read_text())
    assert "running" in json.dumps(recorded)

    # The capture hook is told its own harness and this repository, and
    # reads the conversation its payload names.
    assert shlex.split(command["training_capture"])[2:] == [
        "--harness", harness, "--app-root", str(role_hooks.APP_ROOT)]
    assert shlex.split(command["skill_telemetry"])[2:] == [
        "--harness", harness, "--app-root", str(role_hooks.APP_ROOT)]
    assert shlex.split(command["skill_render_check"])[2:] == [
        sys.executable, str(role_hooks.APP_ROOT)]
    transcript = tmp_path / "conversation.jsonl"
    transcript.write_text("")
    _run(command["training_capture"],
         {"session_id": "s-1", "transcript_path": str(transcript)},
         AUTO_REMEMBER_LOG_DIR=str(state), HOME=str(tmp_path))
    log = state / "training_capture.log"
    deadline = time.monotonic() + 20
    while not log.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert "session=s-1 wrote=False" in log.read_text()


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_installing_again_brings_a_changed_hook_up_to_the_repository(tmp_path, harness):
    home = _home(tmp_path, harness)
    role_hooks.install(harness, home, ["time_inject"], python=sys.executable)
    installed = home / role_hooks.INSTALL_DIR / "time_inject.sh"
    hooks_file = role_hooks.hooks_file_for(harness, home)
    # The mind falls behind: an older script, and a registration with an
    # older timeout.
    installed.write_text("#!/usr/bin/env bash\necho stale\n")
    current = role_hooks.CATALOGUE["time_inject"].timeout
    stale = hooks_file.read_text().replace(
        f"timeout = {current}" if harness == "codex" else f'"timeout": {current}',
        "timeout = 99" if harness == "codex" else '"timeout": 99')
    hooks_file.write_text(stale)
    assert [timeout for *_, timeout in _registered(harness, home)] == [99]

    role_hooks.install(harness, home, ["time_inject"], python=sys.executable)

    assert installed.read_bytes() == (role_hooks.SCRIPTS_DIR / "time_inject.sh").read_bytes()
    registered = _registered(harness, home)
    assert [(event, timeout) for event, _, timeout in registered] == [
        ("UserPromptSubmit", current)]
    assert "Current date and time" in _run(registered[0][1])


def _hand_copy(home: Path, harness: str, *, script: str = "time_inject.sh",
               event: str = "UserPromptSubmit") -> tuple[Path, str]:
    """A mind that keeps its own copy of a hook, registered by hand."""
    hand = home / "hooks" / script
    hand.parent.mkdir()
    hand.write_text("#!/usr/bin/env bash\n"
                    "TZ=Mars/Olympus_Mons date  # the hand copy's own clock\n")
    command = f"bash {hand}"
    hooks_file = role_hooks.hooks_file_for(harness, home)
    if harness == "codex":
        hooks_file.write_text(
            f'model = "x"\n\n[[hooks.{event}]]\n'
            f'[[hooks.{event}.hooks]]\ntype = "command"\n'
            f"command = {json.dumps(command)}\n")
    else:
        hooks_file.write_text(json.dumps({"hooks": {event: [
            {"hooks": [{"type": "command", "command": command}]}]}}, indent=2))
    return hooks_file, command


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_a_hand_copy_is_left_alone_and_its_difference_named(tmp_path, harness):
    home = _home(tmp_path, harness)
    hooks_file, command = _hand_copy(home, harness)
    before = hooks_file.read_text()

    report = role_hooks.install(harness, home, ["time_inject"], python=sys.executable)

    assert hooks_file.read_text() == before
    assert [entry[1] for entry in _registered(harness, home)] == [command]
    outcome = report["hooks"]["time_inject"]
    assert outcome["status"] == "hand_copy_kept"
    lines = [line for found in outcome["differences"] for line in found.get("diff", [])]
    assert "-TZ=Mars/Olympus_Mons date  # the hand copy's own clock" in lines


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_a_hand_copys_difference_is_reported_before_it_is_replaced(tmp_path, harness):
    home = _home(tmp_path, harness)
    hooks_file, command = _hand_copy(home, harness)
    before = hooks_file.read_text()
    told: list[tuple[str, str, list[str]]] = []

    def on_difference(name: str, found: list[dict]) -> None:
        # What the mind's hooks file holds at the moment the difference is
        # handed over: still the hand copy, untouched.
        told.append((name, hooks_file.read_text(),
                     [line for item in found for line in item.get("diff", [])]))

    report = role_hooks.install(harness, home, ["time_inject"], python=sys.executable,
                                replace=True, on_difference=on_difference)

    assert len(told) == 1
    name, file_when_told, lines = told[0]
    assert name == "time_inject"
    assert file_when_told == before
    assert "-TZ=Mars/Olympus_Mons date  # the hand copy's own clock" in lines
    assert report["hooks"]["time_inject"]["status"] == "replaced"
    registered = _registered(harness, home)
    assert len(registered) == 1 and registered[0][1] != command
    assert "Current date and time" in _run(registered[0][1])
    # The hand file is not deleted: it is only no longer what runs.
    assert (home / "hooks" / "time_inject.sh").is_file()


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_hand_copy_under_its_other_name_is_not_registered_beside(tmp_path, harness):
    """Claude and Codex minds keep the telemetry hook as a wrapper with a
    longer name. It is the same hook, so it is a hand copy, not a gap."""
    home = _home(tmp_path, harness)
    hooks_file, command = _hand_copy(home, harness, script="skill_telemetry_capture.sh",
                                     event="Stop")
    before = hooks_file.read_text()

    report = role_hooks.install(harness, home, ["skill_telemetry"], python=sys.executable)

    assert report["hooks"]["skill_telemetry"]["status"] == "hand_copy_kept"
    assert hooks_file.read_text() == before


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_replacing_a_hand_copy_registers_every_event_the_hook_runs_on(tmp_path, harness):
    home = _home(tmp_path, harness)
    _hand_copy(home, harness, script="bg_snapshot.sh", event="Stop")
    (home / "hooks" / "bg_snapshot.py").write_text("# the hand copy's own body\n")

    role_hooks.install(harness, home, ["bg_snapshot"], python=sys.executable, replace=True)

    registered = _registered(harness, home)
    assert sorted(event for event, _, _ in registered) == ["Stop", "SubagentStop"]
    assert {role_hooks._is_ours(command, role_hooks.CATALOGUE["bg_snapshot"], home)
            for _, command, _ in registered} == {True}


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_a_hand_copy_on_an_event_the_hook_does_not_run_on_is_not_replaced(tmp_path, harness):
    """Rewriting it where it stands would leave a prompt hook running on
    Stop, reported as replaced."""
    home = _home(tmp_path, harness)
    hooks_file, _command = _hand_copy(home, harness, event="Stop")
    before = hooks_file.read_text()

    report = role_hooks.install(harness, home, ["time_inject"], python=sys.executable,
                                replace=True)

    assert report["hooks"]["time_inject"]["status"] == "hand_copy_kept"
    assert hooks_file.read_text() == before
