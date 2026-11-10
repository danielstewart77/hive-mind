"""Checks the machine before anything is changed.

The same checks install.sh makes in shell, made again here because the
installer can be started without it. Each failure names what is missing and
the command that fixes it, and nothing has been written when one fails.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MIN_COMPOSE = (2, 20)
MIN_PYTHON = (3, 10)
MIN_DISK_GB = 40


def _run(argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def problems(home_root: Path) -> list[str]:
    out: list[str] = []
    if sys.version_info < MIN_PYTHON:
        out.append(f"Python {'.'.join(map(str, MIN_PYTHON))} or newer is needed — "
                   "install python3 from your distribution")
    if shutil.which("git") is None:
        out.append("git is not installed — fix: sudo apt-get install -y git")
    if shutil.which("docker") is None:
        out.append("Docker is not installed or not on PATH — fix: "
                   "https://docs.docker.com/engine/install/ (then: sudo usermod -aG docker $USER)")
        return out
    info = _run(["docker", "info", "--format", "{{.DockerRootDir}}"])
    if info is None or info.returncode != 0:
        out.append("Docker is not usable without sudo — fix: sudo usermod -aG docker $USER, "
                   "then log out and back in")
        return out
    version = _run(["docker", "compose", "version", "--short"])
    found = re.match(r"v?(\d+)\.(\d+)", (version.stdout if version else "").strip())
    if not found or tuple(map(int, found.groups())) < MIN_COMPOSE:
        out.append(f"Docker Compose {'.'.join(map(str, MIN_COMPOSE))} or newer is needed — fix: "
                   "sudo apt-get install -y docker-compose-plugin")
    buildx = _run(["docker", "buildx", "version"])
    if buildx is None or buildx.returncode != 0:
        out.append("Docker buildx is not installed (the images and the dsh tree are built with "
                   "BuildKit) — fix: sudo apt-get install -y docker-buildx-plugin")
    # The images, volumes and build cache land under Docker's own root, not
    # under the install folder: that is the disk that fills.
    root = Path((info.stdout or "").strip() or "/var/lib/docker")
    for label, path, fix in (
            ("Docker's data folder", root, "free space there or move Docker's data-root"),
            ("the install folder", home_root, "free some space or set HIVE_HOME to a larger disk")):
        try:
            free = shutil.disk_usage(_existing_parent(path)).free / 1024 ** 3
        except OSError:
            continue
        if free < MIN_DISK_GB:
            out.append(f"only {free:.0f} GB free for {label} ({path}); the hive needs "
                       f"{MIN_DISK_GB} GB — {fix}")
    return out
