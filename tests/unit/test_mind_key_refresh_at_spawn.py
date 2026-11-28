"""A replaced key reaches the process a mind starts, with no restart of the mind.

Requirement 21 (protected behaviour 2): the key file is read again at every
spawn, so a turn begun after the console replaced the key runs on the new one.
Each harness's real spawn path is driven with the process launcher (the
boundary where the process stops being ours) replaced, and what is asserted is
the environment handed to the child. The mind server's start is imported in a
fresh interpreter, because its refresh happens when the module loads.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from core import mind_key

OLD = "old-key-0123456789abcdef"  # secret-guard: allow - invented fixture
NEW = "new-key-0123456789abcdef"  # secret-guard: allow - invented fixture
ROOT = Path(__file__).resolve().parents[2]


def _place(path: Path, text: str) -> None:
    temporary = path.with_name(".key.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


@pytest.fixture
def replaced(tmp_path):
    """A mind started on OLD whose key file the console then replaced with NEW."""
    path = tmp_path / "key"
    _place(path, OLD + "\n")
    with mock.patch.dict(os.environ):
        for name in mind_key.GENERIC_NAMES:
            os.environ.pop(name, None)
        os.environ["MIND_KEY_FILE"] = str(path)
        mind_key.refresh()
        assert os.environ["COMMS_BEARER_TOKEN"] == OLD
        _place(path, NEW + "\n")
        yield path


def _handed_to_child(environment: dict) -> None:
    for name in mind_key.GENERIC_NAMES:
        assert environment[name] == NEW, name


class _Proc:
    """Enough of a process for the spawn to return it."""
    returncode = None
    pid = 4321
    stdin = None
    stdout = None
    stderr = None


async def test_a_claude_spawn_hands_the_child_the_replaced_key(replaced) -> None:
    from minds.harness import claude_cli

    seen: dict = {}

    async def launch(*argv, **kwargs):
        seen.update(kwargs)
        return _Proc()

    with mock.patch.object(claude_cli.asyncio, "create_subprocess_exec", new=launch), \
            mock.patch.object(claude_cli, "_drain_stderr", new=mock.AsyncMock()):
        await claude_cli._spawn_proc(
            "s-key", model="sonnet", resume_sid=None, surface_prompt=None,
            allowed_directories=None)

    _handed_to_child(seen["env"])


async def test_a_codex_turn_hands_the_child_the_replaced_key(replaced) -> None:
    from minds.harness import codex_cli

    seen: dict = {}

    async def launch(*argv, **kwargs):
        seen.update(kwargs)
        raise OSError("the launcher stops the turn here")

    codex_cli.SESSIONS.clear()
    codex_cli.SESSIONS["s-key"] = {
        "system_prompt": "system", "thread_id": None, "model": "gpt-5"}
    try:
        with mock.patch.object(codex_cli.asyncio, "create_subprocess_exec", new=launch):
            try:
                _ = [e async for e in codex_cli._run_codex_turn("s-key", "hi", None)]
            except OSError:
                pass
    finally:
        codex_cli.SESSIONS.clear()

    _handed_to_child(seen["env"])


async def test_a_dsh_turn_is_composed_on_the_replaced_key(replaced, monkeypatch) -> None:
    from minds.harness import dsh_cli

    async def window(*args, **kwargs):
        return 131_072

    monkeypatch.setattr(dsh_cli, "_turn_window", window)
    monkeypatch.setattr(dsh_cli, "_role_hooks_env", lambda env: {})
    monkeypatch.setattr(dsh_cli, "_permission_mode", lambda: "default")

    env = await dsh_cli._compose_turn_env("s-key", {"model": "qwen35-131k"})

    _handed_to_child(env)


def test_a_mind_server_starting_reads_the_key_file_over_the_environment(tmp_path) -> None:
    path = tmp_path / "key"
    _place(path, NEW + "\n")
    environment = {k: v for k, v in os.environ.items()
                   if k not in mind_key.GENERIC_NAMES}
    environment.update(MIND_KEY=OLD, MIND_KEY_FILE=str(path),
                       PYTHONPATH=os.pathsep.join([str(ROOT), *[p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]]))
    program = ("import os, json, minds.mind_server;"
               "print(json.dumps({n: os.environ.get(n) for n in "
               "('MIND_KEY', 'COMMS_BEARER_TOKEN', 'LUCENT_BEARER_TOKEN')}))")
    done = subprocess.run([sys.executable, "-c", program], env=environment,
                          capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen == {"MIND_KEY": NEW, "COMMS_BEARER_TOKEN": NEW,
                    "LUCENT_BEARER_TOKEN": NEW}


def test_a_spawn_never_copies_an_environment_half_way_through_a_key_replacement(tmp_path) -> None:
    """While one thread brings the environment to the new key, another taking
    the copy a child is given must see the old key everywhere or the new one
    everywhere, never a mixture."""
    import threading

    path = tmp_path / "key"
    _place(path, OLD + "\n")
    taken: list[dict] = []

    class Environment(dict):
        armed = False

        def __setitem__(self, name, value):
            super().__setitem__(name, value)
            if self.armed and name == "COMMS_BEARER_TOKEN":
                # Mid-update: the first names are new, the rest still old. A
                # spawn that arrives now copies the environment.
                self.armed = False
                copier = threading.Thread(
                    target=lambda: taken.append(mind_key.snapshot(self)))
                copier.start()
                copier.join(timeout=0.3)
                self.copier = copier

    environment = Environment(MIND_KEY_FILE=str(path))
    mind_key.refresh(environment)
    assert environment["MIND_KEY"] == OLD
    _place(path, NEW + "\n")
    environment.armed = True

    mind_key.refresh(environment)
    environment.copier.join(timeout=5)

    assert len(taken) == 1
    held = {taken[0][name] for name in mind_key.GENERIC_NAMES}
    assert len(held) == 1, "a copy taken mid-replacement mixed the old and the new key"
