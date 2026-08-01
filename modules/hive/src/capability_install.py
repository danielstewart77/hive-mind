"""Narrow, fixed-source installation helpers for capability repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

INFERENCE_PROXY_REPOSITORY = "https://github.com/danielstewart77/inference-proxy.git"
DEFAULT_INFERENCE_PROXY_DIR = Path("/inference_proxy_repo")

HIVE_TOOLS_REPOSITORY = "https://github.com/danielstewart77/hive-tools.git"
DEFAULT_HIVE_TOOLS_DIR = Path("/hive_tools_repo")

# Host-side default for the bind mount behind DEFAULT_HIVE_TOOLS_DIR. It is
# relative to the hive checkout so a fresh clone of this repo resolves it on
# any operating system without anyone editing a path; HIVE_TOOLS_DIR in .env
# overrides it, and is the value the card shows and tells the user to change.
DEFAULT_HIVE_TOOLS_HOST_DIR = "./capabilities/hive-tools"

# Compose bind-mounts ${HIVE_TOOLS_DIR}/.env as a single file, so on a host
# where hive-tools was never cloned Docker creates that entry inside the
# destination before anything else runs. It is the hive's own artefact, not
# user content: it neither blocks the install nor gets destroyed by it.
PRESERVED_ENTRIES = frozenset({".env"})

class InstallInProgress(ValueError):
    """Raised when a second install arrives while one is running.

    Distinct from the other ValueErrors here because it is the only transient
    one: retrying in a moment works, where an occupied destination needs a host
    shell first.
    """


# One install at a time. The route runs clone_hive_tools in a worker thread, so
# a double-clicked button puts two of them in this function at once; without the
# lock the second sees the .git the first just created and returns success for a
# working tree that is still empty.
#
# A process-local lock is sufficient because main.py serves this app with a
# single uvicorn process and no --workers. Adding workers would silently
# reopen the hole and needs a filesystem lock instead.
_HIVE_TOOLS_INSTALL_LOCK = threading.Lock()

HIVE_TOOLS_PLACEMENT_NOTE = (
    "Hive Tools is the human approval gate for privileged actions, so clone it "
    "somewhere no non-operator Mind has mounted. Operator Minds have host access "
    "by design and are expected to be what wires up Gmail, SMS and LinkedIn."
)


def inference_proxy_directory() -> Path:
    return Path(
        os.getenv("INFERENCE_PROXY_REPO_DIR") or str(DEFAULT_INFERENCE_PROXY_DIR)
    )


def inference_proxy_status() -> dict:
    path = inference_proxy_directory()
    installed = path.is_dir() and (path / ".git").is_dir()
    return {
        "installed": installed,
        "path": str(path),
        "repository": INFERENCE_PROXY_REPOSITORY,
    }


def clone_inference_proxy() -> dict:
    """Clone the fixed public repository into the mounted capability path."""
    path = inference_proxy_directory()
    if path.is_symlink():
        raise ValueError("The inference proxy destination may not be a symbolic link")
    if (path / ".git").is_dir():
        return inference_proxy_status()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("The inference proxy destination is not empty")
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
    """
    if not path.is_dir() or not (path / ".git").is_dir():
        return False
    try:
        return any(
            entry.name != ".git" and entry.name not in PRESERVED_ENTRIES
            for entry in path.iterdir()
        )
    except OSError:
        return False


def hive_tools_install_status() -> dict:
    return {
        "installed": has_checkout(hive_tools_directory()),
        "path": hive_tools_host_path(),
        "repository": HIVE_TOOLS_REPOSITORY,
    }


def _git(arguments: list[str], *, action: str) -> subprocess.CompletedProcess:
    # GIT_TERMINAL_PROMPT=0 turns an unexpected credential prompt into an error
    # instead of a process that blocks on a tty nobody is attached to. The repo
    # is public, but this container has credentials mounted and HOME=/root.
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed in the Hive container") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"The hive-tools {action} timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or f"Git {action} failed").strip()
        raise RuntimeError(detail[-500:]) from exc
    except OSError as exc:
        # git present but not executable — a noexec mount, a bad bind, SELinux.
        raise RuntimeError(f"Could not run git for the {action}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # text=True decoding git's stderr under a non-UTF-8 locale.
        raise RuntimeError(f"Git returned undecodable output on the {action}") from exc


def _default_branch(repository: str) -> str:
    """Read the remote's own default branch rather than guessing main vs master."""
    completed = _git(["ls-remote", "--symref", repository, "HEAD"], action="lookup")
    for line in (completed.stdout or "").splitlines():
        if line.startswith("ref:") and "\t" in line:
            # Strip only the refs/heads/ prefix — splitting on the last slash
            # would turn a branch named release/v2 into v2, and the fetch of a
            # branch that does not exist fails with a confusing message.
            reference = line.split("\t", 1)[0].split()[1]
            if reference.startswith("refs/heads/"):
                return reference[len("refs/heads/") :]
    raise RuntimeError("Could not determine the hive-tools default branch")


def _rollback(path: Path, preexisting: set[str]) -> list[str]:
    """Return the destination to exactly what was there before the install.

    Removes .git and any half-checked-out files, and touches nothing that was
    already present — the compose-created .env entry survives a failed attempt
    the same way it survives a successful one.

    Returns whatever it could not remove. A rollback that quietly fails is
    worse than one that never ran: the caller's git error would reach the card
    while the node went on reporting a successful install.
    """
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        return [f"{path} ({exc.strerror or exc})"]
    survivors: list[str] = []
    for entry in entries:
        if entry.name in preexisting:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            survivors.append(f"{entry.name} ({exc.strerror or exc})")
    return survivors


def _configured_host_owner() -> tuple[int, int] | None:
    """HOST_UID/HOST_GID as compose passes them, or None when unset."""
    try:
        uid = int(os.getenv("HOST_UID") or "")
    except ValueError:
        return None
    try:
        gid = int(os.getenv("HOST_GID") or "")
    except ValueError:
        gid = uid
    return (uid, gid)


def _adopt_host_ownership(path: Path) -> None:
    """Give the clone away to whoever owns the destination.

    The Hive container runs as root, so every file git writes lands root-owned
    on a host directory the operator owns. They would then hit git's
    dubious-ownership refusal on their own repository and need sudo to remove
    it.

    Two sources for the right uid, in order. HOST_UID/HOST_GID from compose is
    authoritative when set, because the mount point cannot be trusted: when
    HIVE_TOOLS_DIR names a path that does not exist yet — the portable default
    on every fresh checkout — Docker creates the bind source itself, root-owned,
    and matching it would mean adopting root and calling it done. Falling back
    to the mount's own owner covers the case where the operator pre-created the
    directory and compose has no HOST_UID.
    """
    target = _configured_host_owner()
    if target is None:
        try:
            owner = path.stat()
        except OSError:
            return
        if owner.st_uid == 0:
            return
        target = (owner.st_uid, owner.st_gid)
    if target == (os.getuid(), os.getgid()):
        return
    for entry in [path, *path.rglob("*")]:
        try:
            os.chown(entry, target[0], target[1], follow_symlinks=False)
        except OSError:
            # Best effort: a clone the operator has to chown by hand is worse
            # than one they do not, but it is not a failed install.
            continue


def clone_hive_tools() -> dict:
    """Fetch the fixed public repository into the mounted capability path.

    Uses init/fetch/checkout rather than `git clone` because the destination
    is not guaranteed empty — see PRESERVED_ENTRIES. `git clone` refuses any
    non-empty directory, which would fail on precisely the fresh install this
    action exists to serve. That trade costs the atomicity `git clone` provides
    for free, so this restores it by hand: see the cleanup below.
    """
    if not _HIVE_TOOLS_INSTALL_LOCK.acquire(blocking=False):
        raise InstallInProgress("A hive-tools install is already running")
    try:
        return _clone_hive_tools_locked()
    finally:
        _HIVE_TOOLS_INSTALL_LOCK.release()


def _clone_hive_tools_locked() -> dict:
    path = hive_tools_directory()
    if path.is_symlink():
        raise ValueError("The hive-tools destination may not be a symbolic link")
    if has_checkout(path):
        return hive_tools_install_status()
    if path.exists() and not path.is_dir():
        raise ValueError("The hive-tools destination is not a directory")
    try:
        if path.is_dir():
            # `.git` is excluded deliberately: a bare one is the wreckage of an
            # interrupted attempt, not user content, and this action is the way
            # back from it. Everything else present is someone's data.
            occupied = sorted(
                entry.name
                for entry in path.iterdir()
                if entry.name not in PRESERVED_ENTRIES and entry.name != ".git"
            )
            if occupied:
                raise ValueError(
                    "The hive-tools destination is not empty: " + ", ".join(occupied[:5])
                )
        preexisting = (
            {entry.name for entry in path.iterdir() if entry.name != ".git"}
            if path.is_dir()
            else set()
        )
    except OSError as exc:
        # A read-only mount, a bad SELinux label, an NFS hiccup. Surfacing this
        # as a gateway error keeps it in the card's error line instead of a 500.
        raise RuntimeError(f"The hive-tools destination is unreadable: {exc}") from exc

    branch = _default_branch(HIVE_TOOLS_REPOSITORY)
    # `git init` creates .git on the first of four steps, and .git is also the
    # signal every "is hive-tools installed?" check in this codebase reads. A
    # fetch that fails would otherwise leave the console permanently reporting
    # a successful install of an empty directory, with the retry swallowed by
    # the early return above and no way back that does not involve rm -rf on
    # the host. Anything short of a finished checkout is rolled back.
    try:
        _git(["init", "-q", str(path)], action="init")
        _git(
            ["-C", str(path), "remote", "add", "origin", HIVE_TOOLS_REPOSITORY],
            action="remote",
        )
        _git(
            ["-C", str(path), "fetch", "-q", "--depth", "1", "origin", branch],
            action="fetch",
        )
        _git(
            ["-C", str(path), "checkout", "-q", "-B", branch, "FETCH_HEAD"],
            action="checkout",
        )
    except BaseException as exc:
        survivors = _rollback(path, preexisting)
        if survivors:
            raise RuntimeError(
                f"The hive-tools install failed ({exc}) and could not be cleaned "
                f"up. Remove {path} on the host by hand before retrying. "
                "Left behind: " + ", ".join(survivors[:5])
            ) from exc
        raise
    # Give the checkout an upstream. `git clone` sets one; init + fetch +
    # checkout does not, so without this `git pull` in hive-tools fails with
    # "no tracking information" the first time anyone tries to update it.
    with suppress(RuntimeError):
        _git(
            ["-C", str(path), "branch", "--set-upstream-to", f"origin/{branch}", branch],
            action="tracking",
        )
    _adopt_host_ownership(path)
    return hive_tools_install_status()
