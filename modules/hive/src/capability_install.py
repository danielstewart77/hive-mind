"""Narrow, fixed-source installation helpers for capability repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

INFERENCE_PROXY_REPOSITORY = "https://github.com/danielstewart77/inference-proxy.git"
DEFAULT_INFERENCE_PROXY_DIR = Path("/inference_proxy_repo")

# Hive Tools is not installed from here: its source is modules/hive-tools of
# hive-mind, tracked in the same checkout, and the console only reads it
# (read-only mount at /hive_tools_repo). Nothing is fetched from a branch.
DEFAULT_HIVE_TOOLS_DIR = Path("/hive_tools_repo")
DEFAULT_HIVE_TOOLS_HOST_DIR = "../hive-tools"

# Compose bind-mounts a `.env` placeholder inside a destination that was never
# populated; it is the hive's own artefact, not user content, and never counts
# as a checkout's content.
PRESERVED_ENTRIES = frozenset({".env"})

class InstallInProgress(ValueError):
    """Raised when a second install arrives while one is running.

    Distinct from the other ValueErrors here because it is the only transient
    one: retrying in a moment works, where an occupied destination needs a host
    shell first.
    """


# The inference proxy is the other installable node and clones the same way,
# so it needs the same guard for the same reasons.
_INFERENCE_PROXY_INSTALL_LOCK = threading.Lock()

HIVE_TOOLS_PLACEMENT_NOTE = (
    "Hive Tools is the human approval gate for privileged actions. Its source is "
    "part of hive-mind and is built into its image from the committed tree; its "
    "settings and tokens live outside the checkout, where no non-operator Mind "
    "has mounted. Operator Minds have host access by design and are expected to "
    "be what wires up Gmail, SMS and LinkedIn."
)


def inference_proxy_directory() -> Path:
    return Path(
        os.getenv("INFERENCE_PROXY_REPO_DIR") or str(DEFAULT_INFERENCE_PROXY_DIR)
    )


def inference_proxy_install_running() -> bool:
    return _INFERENCE_PROXY_INSTALL_LOCK.locked()


def inference_proxy_status() -> dict:
    return {
        # has_checkout, not a bare .git: an interrupted clone leaves the
        # directory behind and the node would report itself installed forever,
        # which is the state hive-tools was fixed out of.
        "installed": has_checkout(inference_proxy_directory()),
        "installing": inference_proxy_install_running(),
        "path": str(inference_proxy_directory()),
        "repository": INFERENCE_PROXY_REPOSITORY,
    }


def clone_inference_proxy() -> dict:
    if not _INFERENCE_PROXY_INSTALL_LOCK.acquire(blocking=False):
        raise InstallInProgress("An inference proxy install is already running")
    try:
        return _clone_inference_proxy_locked()
    finally:
        _INFERENCE_PROXY_INSTALL_LOCK.release()


def _clone_inference_proxy_locked() -> dict:
    """Clone the fixed public repository into the mounted capability path."""
    path = inference_proxy_directory()
    if path.is_symlink():
        raise ValueError("The inference proxy destination may not be a symbolic link")
    if has_checkout(path):
        return inference_proxy_status()
    if path.exists() and not path.is_dir():
        raise ValueError("The inference proxy destination is not empty")
    if path.is_dir():
        occupied = sorted(entry.name for entry in path.iterdir() if entry.name != ".git")
        if occupied:
            raise ValueError("The inference proxy destination is not empty")
        # A bare .git is the wreckage of an interrupted attempt, not user
        # content, and git clone refuses a directory holding one. Clearing it
        # is the way back — otherwise the retry this action exists to offer
        # fails permanently and only rm -rf on the host recovers.
        shutil.rmtree(path / ".git", ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", INFERENCE_PROXY_REPOSITORY, str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed in the Hive container") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The inference proxy clone timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "Git clone failed").strip()
        raise RuntimeError(detail[-500:]) from exc
    return inference_proxy_status()


def hive_tools_directory() -> Path:
    """Where the clone lands *inside this container* — a fixed bind mount."""
    # `or`, not getenv's default argument: a variable that is set but empty
    # would otherwise resolve to Path("") — the process working directory.
    return Path(os.getenv("HIVE_TOOLS_REPO_DIR") or str(DEFAULT_HIVE_TOOLS_DIR))


def hive_tools_host_path() -> str:
    """The host path behind that mount — what the operator recognises and edits."""
    return os.getenv("HIVE_TOOLS_DIR") or DEFAULT_HIVE_TOOLS_HOST_DIR


def _git_marker(marker: Path) -> bool:
    if marker.is_dir():
        return True
    try:
        return marker.is_file() and marker.read_text(errors="replace").startswith("gitdir:")
    except OSError:
        return False


def has_checkout(path: Path) -> bool:
    """Whether a *finished* clone lives here.

    A bare `.git` is not an install. `git init` creates it on the first of four
    steps, and a fetch that fails — or a SIGKILL from `docker stop` partway
    through one, which no Python `except` can catch — leaves it behind with an
    empty working tree. Reading `.git` alone as the installed signal is what
    made a broken install look permanently successful, with the retry swallowed
    and no route back that did not involve `rm -rf` on the host.

    Requiring real checked-out content alongside it costs nothing, is true of a
    repository the operator cloned by hand, and is false for every partial
    state above.

    A module of hive-mind's manifest is a checkout too: git gives a submodule
    a one-line `.git` *file* naming where its history lives, not a folder.
    """
    if not path.is_dir() or not _git_marker(path / ".git"):
        return False
    try:
        return any(
            entry.name != ".git" and entry.name not in PRESERVED_ENTRIES
            for entry in path.iterdir()
        )
    except OSError:
        return False


def _git_marker(marker: Path) -> bool:
    if marker.is_dir():
        return True
    try:
        return marker.is_file() and marker.read_text(errors="replace").startswith("gitdir:")
    except OSError:
        return False


def has_checkout(path: Path) -> bool:
    """Whether a *finished* clone lives here.

    A bare `.git` is not an install. `git init` creates it on the first of four
    steps, and a fetch that fails — or a SIGKILL from `docker stop` partway
    through one, which no Python `except` can catch — leaves it behind with an
    empty working tree. Reading `.git` alone as the installed signal is what
    made a broken install look permanently successful, with the retry swallowed
    and no route back that did not involve `rm -rf` on the host.

    Requiring real checked-out content alongside it costs nothing, is true of a
    repository the operator cloned by hand, and is false for every partial
    state above.

    A module of hive-mind's manifest is a checkout too: git gives a submodule
    a one-line `.git` *file* naming where its history lives, not a folder.
    """
    if not path.is_dir() or not _git_marker(path / ".git"):
        return False
    try:
        return any(
            entry.name != ".git" and entry.name not in PRESERVED_ENTRIES
            for entry in path.iterdir()
        )
    except OSError:
        return False


def has_hive_tools_source(path: Path) -> bool:
    """Whether hive-mind's own copy of the tools service is readable at `path`.

    The source is plain tracked files, so the signal is the files, not a `.git`.
    """
    try:
        return path.is_dir() and (path / "server.py").is_file() and (path / "Dockerfile").is_file()
    except OSError:
        return False


def hive_tools_install_status() -> dict:
    return {
        "installed": has_hive_tools_source(hive_tools_directory()),
        "installing": False,
        "bundled": True,
        "path": hive_tools_host_path(),
        "repository": "modules/hive-tools in hive-mind",
    }
