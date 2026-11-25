"""Where everything the installer writes lives: under one install folder.

`$HIVE_HOME`, default `~/hive`. Nothing is written outside it — not a
`~/.claude`, not a `/opt/dsh`, not a systemd unit. A mount source that does not
exist when compose starts a service is created by Docker, root-owned, wherever
the path points; so every source is under here and is created first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = "~/hive"

#: The repositories an install uses, by name. hive-mind is cloned; every
#: other one is a module in its manifest (.gitmodules), cloned with it under
#: `modules/<name>` and brought to the latest of its branch by
#: scripts/modules.sh. All public, so no token is needed.
REPOS = (
    "hive-mind", "inference-proxy",
    "hive-harness-dsh",
)
#: Repositories that became ordinary folders of hive-mind and are no longer
#: cloned. `Home.repo(name)` still answers `modules/<name>` for them, so a
#: build context is the same line it always was; the name is kept here only so
#: a clone an earlier install left beside hive-mind is named as unused.
MOVED_IN = ("hive-data", "hive-surfaces", "hive", "terminal", "hive-tools")
#: Where hive-mind keeps its modules, relative to its own checkout.
MODULES = "modules"
REPO_BASE = os.environ.get("HIVE_REPO_BASE", "https://github.com/danielstewart77")


@dataclass(frozen=True)
class Home:
    root: Path

    @classmethod
    def at(cls, path: str | os.PathLike | None = None) -> "Home":
        raw = path or os.environ.get("HIVE_HOME") or DEFAULT_HOME
        return cls(Path(os.path.expanduser(str(raw))).resolve())

    @property
    def repos(self) -> Path:
        return self.root / "repos"

    def repo(self, name: str) -> Path:
        hive_mind = self.repos / "hive-mind"
        return hive_mind if name == "hive-mind" else hive_mind / MODULES / name

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def answers(self) -> Path:
        return self.state / "answers.json"

    @property
    def secrets(self) -> Path:
        return self.state / "secrets.env"

    @property
    def run_json(self) -> Path:
        return self.state / "run.json"

    @property
    def minted(self) -> Path:
        """Which holders this install minted, so a rerun knows its own."""
        return self.state / "minted.json"

    @property
    def page_lock(self) -> Path:
        """Held by the installer process for its whole life."""
        return self.state / "page.lock"

    @property
    def page_link(self) -> Path:
        """The live page's link, 0600, for a second start to reprint."""
        return self.state / "page.link"

    @property
    def log(self) -> Path:
        return self.state / "installer.log"

    @property
    def lock(self) -> Path:
        return self.state / "run.lock"

    @property
    def dsh(self) -> Path:
        return self.root / "dsh"

    @property
    def minds(self) -> Path:
        return self.root / "minds"

    def mind(self, name: str) -> Path:
        return self.minds / name

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def compose_file(self) -> Path:
        return self.root / "docker-compose.yml"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    def ensure(self) -> None:
        """The folders every later write assumes, `state/` readable only by us."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(exist_ok=True)
        os.chmod(self.state, 0o700)
        for folder in (self.repos, self.dsh, self.minds, self.data):
            folder.mkdir(exist_ok=True)


def write_private(path: Path, text: str) -> None:
    """Write a file readable only by its owner, atomically.

    Created 0600 before the first byte goes in, so there is no window in
    which a secret sits in a world-readable file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def write_private_in_place(path: Path, text: str) -> None:
    """Rewrite a private file where it is, keeping the file it already is.

    For a file that a running container has bind-mounted as a single file:
    the hive's `.env`, which the console writes each mind's keys into. A
    rename puts a new file at the path and leaves the container holding the
    old one, so everything it wrote from then on went into a file nothing
    else reads — a mind made on the console came up holding no credential,
    with every step reporting success. Written in place, the container and
    the host go on seeing one file.

    Not atomic, which is the price: a write that stops part way leaves the
    file short. The installer keeps its own copy of every value
    (`state/secrets.env`), written by rename, and a rerun restores from it.
    A file that is not there yet is created the atomic way.
    """
    if not path.is_file() or path.is_symlink():
        write_private(path, text)
        return
    data = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY)
    try:
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
