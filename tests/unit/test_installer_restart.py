"""Running install.sh again while a failed run's page is still open starts the
run again on that page, with the answers it already has.

install.sh is run for real. The one stub is the `docker` command it checks
for, where the process stops being ours; the page is a real SetupServer in
this process, holding the page lock as a live installer does.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from installer.__main__ import hold_page_lock
from installer.runner import Step
from installer.server import SetupServer
from installer.shell import StepFailed

from tests.unit.installer_helpers import answers, installer_at

REPO = Path(__file__).resolve().parents[2]

DOCKER = """#!/bin/sh
case "$1 $2" in
  "info "*) echo "$FAKE_DOCKER_ROOT" ;;
  "compose version") echo "2.29.1" ;;
  "buildx version") echo "github.com/docker/buildx v0.17.1" ;;
esac
"""


def _path_with_docker_stub(tmp_path: Path) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(DOCKER)
    docker.chmod(0o755)
    return f"{bin_dir}:/usr/local/bin:/usr/bin:/bin"


def _install_sh(tmp_path: Path, inst) -> subprocess.CompletedProcess:
    docker_root = tmp_path / "docker-root"
    docker_root.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(REPO / "installer" / "install.sh")], cwd=tmp_path,
        env={"PATH": _path_with_docker_stub(tmp_path), "HOME": str(tmp_path),
             "HIVE_HOME": str(inst.home.root), "FAKE_DOCKER_ROOT": str(docker_root)},
        capture_output=True, text=True, timeout=60)


class _Page:
    """A live installer's page for `inst`: the lock held, the link written."""

    def __init__(self, inst):
        self.lock = hold_page_lock(inst.home)
        self.server = SetupServer(inst, host="127.0.0.1")
        self.server.start()
        self.link = self.server.link("127.0.0.1")
        inst.home.page_link.write_text(self.link + "\n")

    def close(self):
        self.server.stop()
        self.lock.close()


def _wait(until, seconds: float = 10) -> None:
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        time.sleep(0.05)


def test_rerunning_install_sh_while_a_failed_runs_page_is_open_runs_it_again_on_that_page(
        tmp_path):
    attempts = []

    def fails_once(ctx):
        attempts.append(dict(ctx.answers))
        if len(attempts) == 1:
            raise StepFailed("the proxy was unreachable")
        return "ok"

    inst = installer_at(tmp_path, answers(), step_list=[Step("flaky", "Fails once", fails_once, 30)])
    page = _Page(inst)
    try:
        assert inst.start(answers()) == []
        inst._thread.join(30)
        assert inst.runner.snapshot()["failed_step"] == "flaky"

        proc = _install_sh(tmp_path, inst)

        assert proc.returncode == 0, proc.stderr
        assert page.link in proc.stdout
        _wait(lambda: inst.runner.snapshot().get("finished"))
        state = inst.runner.snapshot()
        assert state["finished"] is True and state["failed_step"] is None
        assert len(attempts) == 2
        assert attempts[1]["mind_name"] == attempts[0]["mind_name"]
    finally:
        page.close()


def test_an_older_page_that_cannot_restart_its_failed_run_is_named_with_how_to_stop_it(
        tmp_path):
    def fails(ctx):
        raise StepFailed("the proxy was unreachable")

    inst = installer_at(tmp_path, answers(), step_list=[Step("fails", "Fails", fails, 30)])
    page = _Page(inst)
    # The page as it was before it had a restart route.
    page.server.ROUTES = {k: v for k, v in SetupServer.ROUTES.items() if k[1] != "/api/restart"}
    try:
        assert inst.start(answers()) == []
        inst._thread.join(30)

        proc = _install_sh(tmp_path, inst)

        assert proc.returncode != 0
        assert f"pkill -f 'python3 -m installer --home {inst.home.root}'" in proc.stderr
        assert page.link in proc.stderr
    finally:
        page.close()
