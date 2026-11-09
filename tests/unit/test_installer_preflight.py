"""With no Docker, the installer stops before it changes anything."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _path_without_docker(tmp_path: Path) -> str:
    """A PATH holding every command this host has except docker."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for folder in ("/usr/local/bin", "/usr/bin", "/bin"):
        for entry in sorted(Path(folder).iterdir()) if Path(folder).is_dir() else []:
            target = bin_dir / entry.name
            if entry.name.startswith("docker") or target.exists() or target.is_symlink():
                continue
            target.symlink_to(entry)
    return str(bin_dir)


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_size, p.stat().st_mtime_ns,
                                       p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))} | {".": root.stat().st_mode}


@pytest.mark.parametrize("entry", ["install.sh", "python3 -m installer"])
def test_with_no_docker_the_installer_names_docker_and_leaves_the_folder_as_it_was(
        tmp_path, entry):
    home = tmp_path / "hive"
    (home / "state").mkdir(parents=True)
    (home / "state" / "answers.json").write_text('{"mind_name": "wren"}')
    before = _snapshot(home)
    argv = ["bash", str(REPO / "installer" / "install.sh")] if entry == "install.sh" else \
        ["python3", "-m", "installer", "--home", str(home)]
    env = {"PATH": _path_without_docker(tmp_path), "HOME": str(tmp_path), "HIVE_HOME": str(home)}
    proc = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "Docker" in proc.stderr
    assert _snapshot(home) == before
