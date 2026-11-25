"""scripts/release.sh: put a checkout on a named release, and go back.

The script under test is the repository's own, copied into a scratch hive-mind
(a real git repository cloned from a real bare origin in a temp folder). Docker
is the one thing that stops being ours: HIVE_RELEASE_DOCKER names a stand-in
that logs the arguments it was given, so what the script asks docker to do is
what is asserted, and nothing is built or started.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release.sh"
MODULES = Path(__file__).resolve().parents[2] / "scripts" / "modules.sh"


def git(*args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.invalid")
    for var in ("HIVE_HOME", "HIVE_RELEASE_STATE_DIR", "HIVE_RELEASE_CONTEXT_URL",
                "HIVE_RELEASE_MINDS_REPORT"):
        monkeypatch.delenv(var, raising=False)


class Hive:
    """A scratch install: an origin, a checkout cloned from it, the folders an
    install keeps beside the checkout, and a docker stand-in."""

    def __init__(self, root: Path):
        self.root = root
        origin_work = root / "origin-work"
        origin_work.mkdir()
        git("init", "-q", "-b", "master", cwd=origin_work)
        (origin_work / "scripts").mkdir()
        shutil.copy(SCRIPT, origin_work / "scripts" / "release.sh")
        shutil.copy(MODULES, origin_work / "scripts" / "modules.sh")
        (origin_work / "app.py").write_text("release one\n")
        git("add", "-A", cwd=origin_work)
        git("commit", "-q", "-m", "one", cwd=origin_work)
        git("tag", "v1", cwd=origin_work)
        (origin_work / "app.py").write_text("release two\n")
        git("commit", "-q", "-am", "two", cwd=origin_work)
        git("tag", "v2", cwd=origin_work)
        self.bare = root / "origin.git"
        git("clone", "-q", "--bare", str(origin_work), str(self.bare), cwd=root)
        self.origin_work = origin_work
        git("remote", "add", "origin", str(self.bare), cwd=origin_work)
        self.home = root / "hive"
        self.home.mkdir()
        self.checkout = self.home / "repos" / "hive-mind"
        self.checkout.parent.mkdir()
        git("clone", "-q", str(self.bare), str(self.checkout), cwd=root)
        self.state = self.home / "state"
        self.minds = self.home / "minds"
        self.minds.mkdir()
        (self.minds / "stack.yaml").write_text("include: []\n")
        (self.minds / "ada").mkdir()
        (self.minds / "ada" / "runtime.yaml").write_text("name: ada\n")
        (self.home / ".env").write_text("SECRET=keep-me\n")
        (self.home / "docker-compose.yml").write_text("services: {}\n")
        (self.home / "data").mkdir()
        (self.home / "data" / "rows.db").write_text("rows\n")
        self.no_minds_moved = root / "report-none-moved.sh"
        self.no_minds_moved.write_text('#!/bin/sh\necho \'{"minds": [{"mind": "ada", "state": "not moved"}]}\'\n')
        self.no_minds_moved.chmod(0o755)
        self.docker_log = root / "docker.log"
        self.docker = root / "fake-docker"
        self.docker.write_text(f'#!/bin/sh\necho "$@" >> {self.docker_log}\n')
        self.docker.chmod(self.docker.stat().st_mode | stat.S_IEXEC)

    def commit(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.checkout)

    def tag_commit(self, tag: str) -> str:
        return git("rev-parse", f"{tag}^{{commit}}", cwd=self.checkout)

    def run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        # The console's report: by default, no mind is moved.
        full = {**os.environ, "HIVE_RELEASE_DOCKER": str(self.docker),
                "HIVE_RELEASE_MINDS_REPORT": str(self.no_minds_moved), **(env or {})}
        return subprocess.run(
            ["bash", str(self.checkout / "scripts" / "release.sh"),
             "--state-dir", str(self.state), "--minds-dir", str(self.minds),
             "--compose", str(self.home / "docker-compose.yml"),
             "--env-file", str(self.home / ".env"), "--no-record", *args],
            cwd=self.checkout, capture_output=True, text=True, timeout=120, env=full)

    def docker_calls(self) -> list[str]:
        return self.docker_log.read_text().splitlines() if self.docker_log.exists() else []

    def untouched(self) -> dict[str, bytes]:
        paths = [self.home / ".env", self.home / "docker-compose.yml",
                 self.home / "data" / "rows.db", self.minds / "stack.yaml",
                 self.minds / "ada" / "runtime.yaml"]
        return {str(p): p.read_bytes() for p in paths}


@pytest.fixture
def hive(tmp_path):
    return Hive(tmp_path)


def test_a_release_names_a_commit_and_the_checkout_lands_on_it_detached(hive):
    run = hive.run("to", "v1")

    assert run.returncode == 0, run.stderr
    assert hive.commit() == hive.tag_commit("v1")
    assert (hive.checkout / "app.py").read_text() == "release one\n"
    assert git("branch", "--show-current", cwd=hive.checkout) == ""


def test_going_back_leaves_settings_generated_files_and_data_byte_identical(hive):
    assert hive.run("to", "v1").returncode == 0
    assert hive.run("to", "v2").returncode == 0
    before = hive.untouched()

    run = hive.run("back")

    assert run.returncode == 0, run.stderr
    assert hive.untouched() == before


def test_back_goes_to_the_release_the_checkout_left(hive):
    hive.run("to", "v1")
    assert hive.run("to", "v2").returncode == 0
    leaving = hive.tag_commit("v1")

    run = hive.run("back")

    assert run.returncode == 0, run.stderr
    assert hive.commit() == leaving
    assert (hive.checkout / "app.py").read_text() == "release one\n"
    assert "no migration was run" in run.stdout


def test_back_with_no_release_recorded_refuses_and_changes_nothing(hive):
    before = hive.commit()

    run = hive.run("back")

    assert run.returncode != 0
    assert "previous" in run.stderr
    assert hive.commit() == before


def test_a_branch_is_not_a_release(hive):
    before = hive.commit()

    run = hive.run("to", "origin/master")

    assert run.returncode != 0
    assert "branch" in run.stderr
    assert hive.commit() == before


def test_uncommitted_changes_to_tracked_files_stop_it_and_nothing_moves(hive):
    (hive.checkout / "app.py").write_text("my edit\n")

    run = hive.run("to", "v1")

    assert run.returncode != 0
    assert "uncommitted" in run.stderr
    assert (hive.checkout / "app.py").read_text() == "my edit\n"
    assert not (hive.state / "release" / "current").exists()


def test_a_commit_no_remote_publishes_is_refused(hive):
    (hive.checkout / "app.py").write_text("local only\n")
    git("commit", "-q", "-am", "local only", cwd=hive.checkout)
    local = hive.commit()
    git("checkout", "-q", "--detach", "v1", cwd=hive.checkout)

    run = hive.run("to", local)

    assert run.returncode != 0
    assert "remote" in run.stderr
    assert hive.commit() == hive.tag_commit("v1")


def test_the_state_directory_cannot_be_inside_the_checkout(hive):
    run = subprocess.run(
        ["bash", str(hive.checkout / "scripts" / "release.sh"), "--state-dir",
         str(hive.checkout / "state"), "--no-record", "to", "v1"],
        cwd=hive.checkout, capture_output=True, text=True, timeout=60)

    assert run.returncode != 0
    assert "inside the checkout" in run.stderr


def test_going_to_an_older_release_is_refused_while_a_mind_is_moved_to_its_one_key(hive, tmp_path):
    assert hive.run("to", "v2").returncode == 0
    report = tmp_path / "report.sh"
    report.write_text('#!/bin/sh\necho \'{"minds": [{"mind": "ada", "state": "moved"}, '
                      '{"mind": "bob", "state": "not moved"}]}\'\n')
    report.chmod(0o755)

    run = hive.run("to", "v1", env={"HIVE_RELEASE_MINDS_REPORT": str(report)})

    assert run.returncode != 0
    assert "ada" in run.stderr and "bob" not in run.stderr.split("moved to their one key")[1]
    assert hive.commit() == hive.tag_commit("v2")


def test_going_to_an_older_release_is_refused_when_the_minds_report_cannot_be_read(hive, tmp_path):
    assert hive.run("to", "v2").returncode == 0
    report = tmp_path / "broken.sh"
    report.write_text("#!/bin/sh\nexit 3\n")
    report.chmod(0o755)

    run = hive.run("to", "v1", env={"HIVE_RELEASE_MINDS_REPORT": str(report)})

    assert run.returncode != 0
    assert hive.commit() == hive.tag_commit("v2")


def test_the_operator_who_checked_the_minds_themselves_can_go_back_with_the_console_down(hive, tmp_path):
    assert hive.run("to", "v2").returncode == 0
    report = tmp_path / "broken.sh"
    report.write_text("#!/bin/sh\nexit 3\n")
    report.chmod(0o755)

    run = hive.run("--minds-checked", "to", "v1", env={"HIVE_RELEASE_MINDS_REPORT": str(report)})

    assert run.returncode == 0, run.stderr
    assert hive.commit() == hive.tag_commit("v1")


def test_going_to_an_older_release_goes_ahead_when_no_mind_is_moved(hive, tmp_path):
    assert hive.run("to", "v2").returncode == 0
    report = tmp_path / "report.sh"
    report.write_text('#!/bin/sh\necho \'{"minds": [{"mind": "ada", "state": "not moved"}]}\'\n')
    report.chmod(0o755)

    run = hive.run("to", "v1", env={"HIVE_RELEASE_MINDS_REPORT": str(report)})

    assert run.returncode == 0, run.stderr
    assert hive.commit() == hive.tag_commit("v1")


def test_the_console_is_told_the_commit_new_minds_are_built_from(hive):
    run = hive.run("to", "v1", env={"HIVE_RELEASE_CONTEXT_URL": "https://example.invalid/hive-mind.git"})

    assert run.returncode == 0, run.stderr
    assert (hive.minds / ".release-context").read_text().strip() == \
        f"https://example.invalid/hive-mind.git#{hive.tag_commit('v1')}"


def test_the_release_file_never_carries_a_credential_from_the_remote_address(hive):
    git("remote", "set-url", "origin", "https://user:s3cret@example.invalid/hive-mind.git", cwd=hive.checkout)
    run = hive.run("to", "v1", env={"HIVE_RELEASE_NO_FETCH": "1"})

    assert run.returncode == 0, run.stderr
    assert "s3cret" not in (hive.minds / ".release-context").read_text()


def test_it_builds_the_stack_and_by_default_recreates_nothing(hive):
    assert hive.run("to", "v1").returncode == 0

    calls = hive.docker_calls()

    assert any(" build" in c or c.endswith("build") for c in calls)
    assert not any(" up " in f" {c} " for c in calls)
    assert not any(w in c.split() for c in calls for w in ("down", "rm", "volume", "prune"))


def test_with_restart_it_recreates_the_stack_after_building(hive):
    assert hive.run("--restart", "to", "v1").returncode == 0

    calls = hive.docker_calls()

    assert any(c.split()[-2:] == ["up", "-d"] for c in calls)
    assert [i for i, c in enumerate(calls) if c.endswith("build")][0] < \
        [i for i, c in enumerate(calls) if c.endswith("up -d")][0]


def test_a_dry_run_changes_nothing(hive):
    before = hive.commit()

    run = hive.run("--dry-run", "to", "v1")

    assert run.returncode == 0, run.stderr
    assert hive.commit() == before
    assert hive.docker_calls() == []
    assert not (hive.minds / ".release-context").exists()


def test_an_old_clone_at_a_path_the_release_holds_as_files_is_moved_aside_when_it_holds_nothing_unsaved(hive):
    # The release carries modules/hive-data as files; this machine has an old clone there.
    work = hive.origin_work
    (work / "modules" / "hive-data").mkdir(parents=True)
    (work / "modules" / "hive-data" / "pkg.py").write_text("files\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "hive-data moves in", cwd=work)
    git("tag", "v3", cwd=work)
    git("push", "-q", "origin", "master", "--tags", cwd=work)
    old = hive.checkout / "modules" / "hive-data"
    git("clone", "-q", str(hive.bare), str(old), cwd=hive.root)
    (old / ".git" / "info" / "exclude").write_text(".env\n")  # as the real clones ignore it
    (old / ".env").write_text("TOKEN=carry-me\n")

    run = hive.run("to", "v3")

    assert run.returncode == 0, run.stderr
    assert (hive.checkout / "modules" / "hive-data" / "pkg.py").read_text() == "files\n"
    aside = [p for p in (hive.checkout / "modules").iterdir() if ".moved-aside-" in p.name]
    assert len(aside) == 1 and (aside[0] / ".env").read_text() == "TOKEN=carry-me\n"


def test_an_old_clone_holding_unsaved_work_stops_the_move_and_is_left_where_it_is(hive):
    work = hive.origin_work
    (work / "modules" / "hive-data").mkdir(parents=True)
    (work / "modules" / "hive-data" / "pkg.py").write_text("files\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "hive-data moves in", cwd=work)
    git("tag", "v3", cwd=work)
    git("push", "-q", "origin", "master", "--tags", cwd=work)
    old = hive.checkout / "modules" / "hive-data"
    git("clone", "-q", str(hive.bare), str(old), cwd=hive.root)
    (old / "app.py").write_text("unsaved edit\n")
    before = hive.commit()

    run = hive.run("to", "v3")

    assert run.returncode != 0
    assert "hive-data" in run.stderr
    assert (old / "app.py").read_text() == "unsaved edit\n"
    assert hive.commit() == before


def test_going_back_also_returns_a_module_kept_in_its_own_repository_to_the_commit_the_old_release_records(hive, tmp_path):
    sub = tmp_path / "sub-work"
    sub.mkdir()
    git("init", "-q", "-b", "main", cwd=sub)
    (sub / "version").write_text("1")
    git("add", "-A", cwd=sub)
    git("commit", "-q", "-m", "s1", cwd=sub)
    first = git("rev-parse", "HEAD", cwd=sub)
    sub_bare = tmp_path / "sub.git"
    git("clone", "-q", "--bare", str(sub), str(sub_bare), cwd=tmp_path)
    work = hive.origin_work
    git("-c", "protocol.file.allow=always", "submodule", "add", "-q", "-b", "main",
        f"file://{sub_bare}", "modules/alpha", cwd=work)
    git("commit", "-q", "-m", "release records alpha at one", cwd=work)
    git("tag", "v4", cwd=work)
    (sub / "version").write_text("2")
    git("commit", "-q", "-am", "s2", cwd=sub)
    git("push", "-q", str(sub_bare), "HEAD:main", cwd=sub)
    second = git("rev-parse", "HEAD", cwd=sub)
    git("-c", "protocol.file.allow=always", "submodule", "update", "--remote", "-q", cwd=work)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "release records alpha at two", cwd=work)
    git("tag", "v5", cwd=work)
    git("push", "-q", "origin", "master", "--tags", cwd=work)
    module = hive.checkout / "modules" / "alpha"

    assert hive.run("to", "v5").returncode == 0
    assert git("rev-parse", "HEAD", cwd=module) == second
    run = hive.run("to", "v4")

    assert run.returncode == 0, run.stderr
    assert git("rev-parse", "HEAD", cwd=module) == first
