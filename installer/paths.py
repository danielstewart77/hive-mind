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

#: The repositories an install may clone, by the name each is cloned under.
#: All public, so no token is needed.
REPOS = (
    "hive-mind", "hive-data", "hive", "terminal", "inference-proxy",
    "hive-tools", "hive-harness-dsh",
)
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
        return self.repos / name

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
