"""Capability repository installation is fixed-source and non-destructive."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import capability_install


def test_clone_inference_proxy_uses_fixed_public_repository(tmp_path, monkeypatch):
    destination = tmp_path / "inference-proxy"
    monkeypatch.setenv("INFERENCE_PROXY_REPO_DIR", str(destination))

    def clone(command, **kwargs):
        assert command == [
            "git",
            "clone",
            "--depth",
            "1",
            capability_install.INFERENCE_PROXY_REPOSITORY,
            str(destination),
        ]
        assert kwargs["timeout"] == 120
        (destination / ".git").mkdir(parents=True)
        # A finished clone leaves checked-out content beside .git; a bare .git
        # is the wreckage of an interrupted one and is not an install.
        (destination / "README.md").write_text("# inference-proxy\n")

    with patch.object(capability_install.subprocess, "run", side_effect=clone):
        result = capability_install.clone_inference_proxy()

    assert result["installed"] is True
    assert result["repository"].startswith("https://github.com/")


def test_clone_inference_proxy_refuses_nonempty_destination(tmp_path, monkeypatch):
    destination = tmp_path / "inference-proxy"
    destination.mkdir()
    (destination / "keep.txt").write_text("mine")
    monkeypatch.setenv("INFERENCE_PROXY_REPO_DIR", str(destination))

    with pytest.raises(ValueError, match="not empty"):
        capability_install.clone_inference_proxy()

    assert (destination / "keep.txt").read_text() == "mine"


def test_clone_inference_proxy_is_idempotent(tmp_path, monkeypatch):
    destination = tmp_path / "inference-proxy"
    (destination / ".git").mkdir(parents=True)
    (destination / "README.md").write_text("# inference-proxy\n")
    monkeypatch.setenv("INFERENCE_PROXY_REPO_DIR", str(destination))

    with patch.object(capability_install.subprocess, "run") as clone:
        result = capability_install.clone_inference_proxy()

    clone.assert_not_called()
    assert result["installed"] is True


# ── hive-tools ────────────────────────────────────────────────────────
#
# One test per numbered requirement of the Capability Nodes clone action.


def _fake_git(destination, *, branch="master"):
    """Stand in for git, including the parts that put files on disk.

    `init` creates .git, `fetch` records that objects arrived, and `checkout`
    writes a working tree — but only if a fetch actually preceded it, the way
    real git would fail on FETCH_HEAD that does not exist. A fake that always
    produced .git and never produced content let the step order be scrambled
    without a single test noticing.
    """
    state = {"fetched": False}

    def run(command, **kwargs):
        arguments = command[1:]
        if arguments[0] == "ls-remote":
            return SimpleNamespace(stdout=f"ref: refs/heads/{branch}\tHEAD\n", stderr="")
        if arguments[0] == "init":
            (destination / ".git").mkdir(parents=True, exist_ok=True)
        elif "fetch" in arguments:
            if not (destination / ".git").is_dir():
                raise subprocess.CalledProcessError(
                    128, command, stderr="fatal: not a git repository"
                )
            state["fetched"] = True
        elif "checkout" in arguments:
            if not state["fetched"]:
                raise subprocess.CalledProcessError(
                    128, command, stderr="fatal: FETCH_HEAD not a valid object name"
                )
            (destination / "README.md").write_text("# hive-tools\n")
            (destination / "tools").mkdir(exist_ok=True)
        return SimpleNamespace(stdout="", stderr="")

    return run


# Requirement 1 — the clone action installs the repository and the node then
# reports hive-tools as installed.
def test_clone_hive_tools_installs_into_the_configured_destination(
    tmp_path, monkeypatch
):
    destination = tmp_path / "hive-tools"
    destination.mkdir()
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    commands = []
    fake = _fake_git(destination)

    def run(command, **kwargs):
        commands.append(command[1:])
        return fake(command, **kwargs)

    with patch.object(capability_install.subprocess, "run", side_effect=run):
        result = capability_install.clone_hive_tools()

    assert result["installed"] is True
    # A real checked-out working tree, not just the .git the first step makes.
    assert (destination / "README.md").read_text() == "# hive-tools\n"
    # Literal, not the constant under test: which repository this installs is
    # the whole point of a fixed-source installer, and comparing the value to
    # itself would let it be repointed anywhere without a test noticing.
    assert result["repository"] == "https://github.com/danielstewart77/hive-tools.git"
    assert "https://github.com/danielstewart77/hive-tools.git" in commands[2]
    assert [arguments[0] for arguments in commands] == [
        "ls-remote",
        "init",
        "-C",
        "-C",
        "-C",
        "-C",
    ]


# Requirement 1, failure path — a git step that fails partway leaves nothing
# behind, so the node still reports not-installed and the button still works.
def test_clone_hive_tools_reports_not_installed_after_a_failed_fetch(
    tmp_path, monkeypatch
):
    destination = tmp_path / "hive-tools"
    (destination / ".env").mkdir(parents=True)
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))

    def run(command, **kwargs):
        arguments = command[1:]
        if arguments[0] == "ls-remote":
            return SimpleNamespace(stdout="ref: refs/heads/master\tHEAD\n", stderr="")
        if arguments[0] == "init":
            (destination / ".git").mkdir(parents=True, exist_ok=True)
            (destination / ".git" / "config").write_text("[core]\n")
            return SimpleNamespace(stdout="", stderr="")
        if "fetch" in arguments:
            raise capability_install.subprocess.CalledProcessError(
                128, command, stderr="fatal: could not resolve host"
            )
        return SimpleNamespace(stdout="", stderr="")

    with patch.object(capability_install.subprocess, "run", side_effect=run):
        with pytest.raises(RuntimeError, match="could not resolve host"):
            capability_install.clone_hive_tools()

    assert not (destination / ".git").exists()
    assert capability_install.hive_tools_install_status()["installed"] is False
    assert (destination / ".env").exists()

    # And the retry is a real retry, not the early return swallowing it.
    with patch.object(
        capability_install.subprocess, "run", side_effect=_fake_git(destination)
    ) as retried:
        assert capability_install.clone_hive_tools()["installed"] is True
    assert retried.called


# Requirement 1, concurrency — a second install arriving mid-clone is refused
# rather than answered with a success the working tree does not back up.
def test_clone_hive_tools_refuses_a_second_concurrent_install(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    destination.mkdir()
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    observed = {}
    fake = _fake_git(destination)

    def run(command, **kwargs):
        if command[1] == "init" and "reentered" not in observed:
            observed["reentered"] = True
            with pytest.raises(
                capability_install.InstallInProgress, match="already running"
            ):
                capability_install.clone_hive_tools()
        return fake(command, **kwargs)

    with patch.object(capability_install.subprocess, "run", side_effect=run):
        assert capability_install.clone_hive_tools()["installed"] is True
    assert observed["reentered"] is True


# Requirement 2 — the reported destination comes from HIVE_TOOLS_DIR, and
# falls back to the portable relative default when that is unset.
def test_hive_tools_status_reports_the_configured_host_path(monkeypatch):
    monkeypatch.setenv("HIVE_TOOLS_DIR", "/srv/private/hive-tools")
    assert capability_install.hive_tools_install_status()["path"] == (
        "/srv/private/hive-tools"
    )

    # Literal, not the constant: the requirement is that the fallback is a
    # relative path portable to any host, which an assertion against the
    # constant itself cannot tell apart from someone's home directory.
    monkeypatch.delenv("HIVE_TOOLS_DIR")
    fallback = capability_install.hive_tools_install_status()["path"]
    assert fallback == "./capabilities/hive-tools"
    assert not Path(fallback).is_absolute()


# Requirement 2, fresh install — an operator who copies .env.example verbatim,
# as the README tells them to, lands on the portable relative default rather
# than on a path belonging to whoever wrote the example file.
def test_a_fresh_env_example_resolves_to_the_portable_default(monkeypatch):
    example = Path(__file__).resolve().parent.parent / ".env.example"
    settings = dict(
        line.split("=", 1)
        for line in example.read_text().splitlines()
        if "=" in line and not line.strip().startswith("#")
    )
    monkeypatch.delenv("HIVE_TOOLS_DIR", raising=False)
    for key, value in settings.items():
        monkeypatch.setenv(key.strip(), value.strip())

    resolved = capability_install.hive_tools_host_path()

    assert resolved == "./capabilities/hive-tools"
    assert not Path(resolved).is_absolute()


# Requirement 1 — clicking again on an installed hive-tools does nothing to the
# checkout that is already there, rather than re-initialising over live files.
def test_clone_hive_tools_is_idempotent_once_installed(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    (destination / ".git").mkdir(parents=True)
    (destination / "README.md").write_text("# hive-tools\n")
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))

    with patch.object(capability_install.subprocess, "run") as git:
        result = capability_install.clone_hive_tools()

    git.assert_not_called()
    assert result["installed"] is True
    assert (destination / "README.md").read_text() == "# hive-tools\n"


# Requirement 4 — a destination occupied by a regular file is refused too, not
# only one occupied by a directory of someone else's content.
def test_clone_hive_tools_refuses_a_file_at_the_destination(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    destination.write_text("not a directory")
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))

    with pytest.raises(ValueError, match="not a directory"):
        capability_install.clone_hive_tools()

    assert destination.read_text() == "not a directory"


# Requirement 1 — the branch the remote actually names is the one fetched, so
# a repository whose default branch contains a slash still installs.
def test_clone_hive_tools_fetches_a_slashed_default_branch(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    destination.mkdir()
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    commands = []
    fake = _fake_git(destination, branch="release/v2")

    def run(command, **kwargs):
        commands.append(command[1:])
        return fake(command, **kwargs)

    with patch.object(capability_install.subprocess, "run", side_effect=run):
        assert capability_install.clone_hive_tools()["installed"] is True

    fetch = next(arguments for arguments in commands if "fetch" in arguments)
    assert "release/v2" in fetch


# Requirement 1 — an install interrupted by process death, not by an exception,
# still leaves the node reporting not-installed so the button comes back.
def test_a_bare_git_left_by_a_killed_install_does_not_read_as_installed(
    tmp_path, monkeypatch
):
    destination = tmp_path / "hive-tools"
    (destination / ".git").mkdir(parents=True)
    (destination / ".env").mkdir()
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))

    assert capability_install.hive_tools_install_status()["installed"] is False

    # And the retry resumes rather than early-returning on that .git.
    with patch.object(
        capability_install.subprocess, "run", side_effect=_fake_git(destination)
    ) as resumed:
        assert capability_install.clone_hive_tools()["installed"] is True
    assert resumed.called
    assert (destination / ".env").exists()


# Requirement 1 — a rollback that cannot finish says so, instead of letting the
# node go on reporting an install that is not there.
def test_a_failed_rollback_is_reported_rather_than_swallowed(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    destination.mkdir()
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))

    def run(command, **kwargs):
        arguments = command[1:]
        if arguments[0] == "ls-remote":
            return SimpleNamespace(stdout="ref: refs/heads/master\tHEAD\n", stderr="")
        if arguments[0] == "init":
            (destination / ".git").mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(stdout="", stderr="")
        if "fetch" in arguments:
            raise subprocess.CalledProcessError(128, command, stderr="network down")
        return SimpleNamespace(stdout="", stderr="")

    with patch.object(capability_install.subprocess, "run", side_effect=run):
        with patch.object(
            capability_install.shutil, "rmtree", side_effect=OSError("device busy")
        ):
            with pytest.raises(RuntimeError, match="could not be cleaned up"):
                capability_install.clone_hive_tools()


# Requirement 1 — the cloned tree belongs to the operator, not to the root the
# container happens to run as, or they cannot maintain their own repository.
def test_clone_hive_tools_hands_the_checkout_to_the_host_owner(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    destination.mkdir()
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    monkeypatch.setenv("HOST_UID", "1234")
    monkeypatch.setenv("HOST_GID", "5678")
    chowned = []

    with patch.object(capability_install.os, "chown", side_effect=lambda p, u, g, **k: chowned.append((Path(p).name, u, g))):
        with patch.object(
            capability_install.subprocess, "run", side_effect=_fake_git(destination)
        ):
            capability_install.clone_hive_tools()

    assert chowned, "the clone was left owned by the container user"
    assert {(uid, gid) for _, uid, gid in chowned} == {(1234, 5678)}
    assert "README.md" in {name for name, _, _ in chowned}


# Requirement 1 — git is never allowed to stop and wait for a credential
# prompt on a terminal no one is attached to.
def test_clone_hive_tools_disables_git_terminal_prompts(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    destination.mkdir()
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    environments = []
    fake = _fake_git(destination)

    def run(command, **kwargs):
        environments.append(kwargs.get("env") or {})
        return fake(command, **kwargs)

    with patch.object(capability_install.subprocess, "run", side_effect=run):
        capability_install.clone_hive_tools()

    assert environments
    assert all(env.get("GIT_TERMINAL_PROMPT") == "0" for env in environments)


# Requirement 3 lives in tests/test_capability_nodes.py — it is about what the
# card renders from, not about the clone helper.


# Requirement 4 — a symlink or an occupied destination is refused, untouched.
def test_clone_hive_tools_refuses_symlink_and_occupied_destinations(
    tmp_path, monkeypatch
):
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path / "elsewhere")
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(linked))
    with pytest.raises(ValueError, match="symbolic link"):
        capability_install.clone_hive_tools()

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("mine")
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(occupied))
    with pytest.raises(ValueError, match="not empty"):
        capability_install.clone_hive_tools()

    assert [entry.name for entry in occupied.iterdir()] == ["keep.txt"]
    assert (occupied / "keep.txt").read_text() == "mine"


# Requirement 5 — the compose-created .env placeholder neither blocks the
# install nor is destroyed by it.
def test_clone_hive_tools_installs_alongside_the_env_placeholder(
    tmp_path, monkeypatch
):
    destination = tmp_path / "hive-tools"
    (destination / ".env").mkdir(parents=True)
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))

    with patch.object(
        capability_install.subprocess, "run", side_effect=_fake_git(destination)
    ):
        result = capability_install.clone_hive_tools()

    assert result["installed"] is True
    assert (destination / ".env").exists()
