"""Only the tools service holds the host's Docker socket (requirement 9)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from installer import compose
from tests.unit.installer_helpers import a_host, all_pieces, answers, installer_at

ROOT = Path(__file__).resolve().parents[2]
SOCKET = "docker.sock"


def _mounts_socket(service: dict) -> bool:
    for volume in service.get("volumes") or []:
        source = volume.get("source", "") if isinstance(volume, dict) else str(volume).split(":", 1)[0]
        # The socket itself, or a parent that carries it with it.
        if source.endswith(SOCKET) or source.rstrip("/") in ("", "/var/run", "/run"):
            return True
    return False


def _holders(document: dict) -> set[str]:
    return {name for name, svc in (document.get("services") or {}).items() if _mounts_socket(svc)}


def _tracked_compose_files() -> list[Path]:
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                            check=True).stdout.split()
    return [ROOT / f for f in listed
            if f.endswith((".yml", ".yaml")) and "compose" in Path(f).name]


def test_in_every_tracked_compose_file_only_the_tools_service_mounts_the_docker_socket():
    files = _tracked_compose_files()
    assert ROOT / "modules" / "hive-tools" / "docker-compose.yml" in files
    found = {}
    for path in files:
        holders = _holders(yaml.safe_load(path.read_text()))
        if holders:
            found[str(path.relative_to(ROOT))] = holders
    assert found == {"modules/hive-tools/docker-compose.yml": {"hive-tools"}}


def test_in_the_installers_rendered_stack_only_the_tools_service_mounts_the_docker_socket(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=all_pieces(), modes={"voice": "here"},
                                          telegram_token="t", discord_token="d"), host=a_host())
    rendered = compose.render(inst.home, inst.answers, inst.host)
    assert _holders(rendered.compose) == {"hive-tools"}
