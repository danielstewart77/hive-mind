"""hive-mind's manifest: cloning it clones every module, each at the commit
the release records for it; following a branch tip is an explicit opt-in.

Real git, against bare repositories in a temp folder standing in for GitHub.
The script under test is the repository's own `scripts/modules.sh`, run on a
fixture superproject; the installer's clone step is the installer's own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from installer import steps
from installer.shell import Shell

from tests.unit.installer_helpers import answers, installer_at

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "modules.sh"


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    # Local file remotes are refused for submodules unless allowed; and the
    # fixture commits need an identity.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.invalid")
    monkeypatch.delenv("HIVE_REPO_BASE", raising=False)


def git(*args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class Remotes:
    """Bare repositories under one base, the way GitHub holds them."""

    def __init__(self, root: Path):
        self.base = root / "remotes"
        self.base.mkdir()
        self.scratch = root / "scratch"
        self.scratch.mkdir()

    def url(self, name: str) -> str:
        return f"file://{self.base}/{name}.git"

    def create(self, name: str, branch: str) -> str:
        work = self.scratch / name
        work.mkdir()
        git("init", "-q", "-b", branch, cwd=work)
        (work / "version").write_text("1")
        git("add", "version", cwd=work)
        git("commit", "-q", "-m", "first", cwd=work)
        git("clone", "-q", "--bare", str(work), str(self.base / f"{name}.git"), cwd=self.scratch)
        git("remote", "add", "origin", self.url(name), cwd=work)
        return git("rev-parse", "HEAD", cwd=work)

    def push_new(self, name: str, content: str) -> str:
        work = self.scratch / name
        (work / "version").write_text(content)
        git("commit", "-q", "-am", content, cwd=work)
        git("push", "-q", "origin", "HEAD", cwd=work)
        return git("rev-parse", "HEAD", cwd=work)

    def superproject(self, modules: dict[str, str], *, with_script: bool = False) -> str:
        """A hive-mind recording each module at its first commit."""
        work = self.scratch / "hive-mind"
        work.mkdir()
        git("init", "-q", "-b", "master", cwd=work)
        for name, branch in modules.items():
            git("submodule", "add", "-q", "-b", branch, self.url(name), f"modules/{name}", cwd=work)
        if with_script:
            (work / "scripts").mkdir()
            shutil.copy(SCRIPT, work / "scripts" / "modules.sh")
            git("add", "scripts", cwd=work)
        git("commit", "-q", "-m", "manifest", cwd=work)
        git("clone", "-q", "--bare", str(work), str(self.base / "hive-mind.git"), cwd=self.scratch)
        return self.url("hive-mind")


def modules_sh(checkout: Path, *, float_to_tips: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if float_to_tips:
        env["HIVE_MODULES_FLOAT"] = "1"
    return subprocess.run(["bash", str(SCRIPT), str(checkout)], capture_output=True, text=True,
                          timeout=120, env=env)


@pytest.fixture
def hive(tmp_path):
    remotes = Remotes(tmp_path)
    remotes.create("alpha", "main")
    remotes.create("beta", "master")
    url = remotes.superproject({"alpha": "main", "beta": "master"})
    checkout = tmp_path / "checkout"
    git("clone", "-q", url, str(checkout), cwd=tmp_path)
    return remotes, checkout


def test_every_module_is_cloned_at_the_commit_the_release_records_not_the_tip_of_its_branch(hive):
    remotes, checkout = hive
    recorded = {n: git("rev-parse", f"HEAD:modules/{n}", cwd=checkout) for n in ("alpha", "beta")}
    remotes.push_new("alpha", "2")
    remotes.push_new("beta", "2")

    run = modules_sh(checkout)

    assert run.returncode == 0, run.stderr
    for name in ("alpha", "beta"):
        module = checkout / "modules" / name
        assert git("rev-parse", "HEAD", cwd=module) == recorded[name]
        assert git("branch", "--show-current", cwd=module) == ""  # detached: a release is a commit


def test_a_checkout_moved_to_another_release_brings_each_module_to_that_releases_commit(hive):
    remotes, checkout = hive
    assert modules_sh(checkout).returncode == 0
    newer = remotes.push_new("alpha", "next release")
    git("update-index", "--cacheinfo", f"160000,{newer},modules/alpha", cwd=checkout)
    git("commit", "-q", "-m", "release records a newer alpha", cwd=checkout)

    run = modules_sh(checkout)

    assert run.returncode == 0, run.stderr
    assert git("rev-parse", "HEAD", cwd=checkout / "modules" / "alpha") == newer


def test_a_module_holding_work_no_remote_has_is_not_moved_off_it_and_the_run_fails(hive):
    remotes, checkout = hive
    assert modules_sh(checkout).returncode == 0
    alpha = checkout / "modules" / "alpha"
    git("checkout", "-q", "--detach", cwd=alpha)
    (alpha / "version").write_text("detached work")
    git("commit", "-q", "-am", "detached work", cwd=alpha)
    work = git("rev-parse", "HEAD", cwd=alpha)
    newer = remotes.push_new("alpha", "next release")
    git("update-index", "--cacheinfo", f"160000,{newer},modules/alpha", cwd=checkout)
    git("commit", "-q", "-m", "release records a newer alpha", cwd=checkout)

    run = modules_sh(checkout)

    assert run.returncode != 0
    assert "modules/alpha" in run.stderr
    assert git("rev-parse", "HEAD", cwd=alpha) == work


def test_with_the_float_opt_in_every_module_is_brought_to_the_newest_commit_of_its_branch_and_left_on_it(hive):
    remotes, checkout = hive
    newest_alpha = remotes.push_new("alpha", "2")
    newest_beta = remotes.push_new("beta", "2")

    run = modules_sh(checkout, float_to_tips=True)

    assert run.returncode == 0, run.stderr
    alpha, beta = checkout / "modules" / "alpha", checkout / "modules" / "beta"
    assert git("rev-parse", "HEAD", cwd=alpha) == newest_alpha
    assert git("rev-parse", "HEAD", cwd=beta) == newest_beta
    assert git("branch", "--show-current", cwd=alpha) == "main"
    assert git("branch", "--show-current", cwd=beta) == "master"


def test_a_second_run_fast_forwards_a_module_already_cloned(hive):
    remotes, checkout = hive
    assert modules_sh(checkout, float_to_tips=True).returncode == 0
    newest = remotes.push_new("alpha", "3")

    run = modules_sh(checkout, float_to_tips=True)

    assert run.returncode == 0, run.stderr
    assert git("rev-parse", "HEAD", cwd=checkout / "modules" / "alpha") == newest


def test_a_module_left_on_another_branch_is_not_moved(hive):
    remotes, checkout = hive
    assert modules_sh(checkout).returncode == 0
    alpha = checkout / "modules" / "alpha"
    remotes.push_new("alpha", "past-recorded")
    assert modules_sh(checkout, float_to_tips=True).returncode == 0
    git("checkout", "-q", "-b", "feature", cwd=alpha)
    (alpha / "version").write_text("feature work")
    git("commit", "-q", "-am", "feature work", cwd=alpha)
    before = git("rev-parse", "HEAD", cwd=alpha)
    remotes.push_new("alpha", "4")

    run = modules_sh(checkout, float_to_tips=True)

    assert run.returncode == 0, run.stderr
    assert git("branch", "--show-current", cwd=alpha) == "feature"
    assert git("rev-parse", "HEAD", cwd=alpha) == before


def test_a_pinned_module_stays_at_its_pin(hive):
    remotes, checkout = hive
    pinned = remotes.push_new("alpha", "5")
    remotes.push_new("alpha", "after the pin")
    git("config", "-f", ".gitmodules", "submodule.modules/alpha.pin", pinned, cwd=checkout)

    run = modules_sh(checkout, float_to_tips=True)

    assert run.returncode == 0, run.stderr
    assert git("rev-parse", "HEAD", cwd=checkout / "modules" / "alpha") == pinned


def test_a_module_whose_branch_diverged_is_named_and_the_run_fails(hive):
    remotes, checkout = hive
    assert modules_sh(checkout, float_to_tips=True).returncode == 0
    alpha = checkout / "modules" / "alpha"
    (alpha / "version").write_text("local")
    git("commit", "-q", "-am", "local only", cwd=alpha)
    remotes.push_new("alpha", "6")
    newest_beta = remotes.push_new("beta", "6")

    run = modules_sh(checkout, float_to_tips=True)

    assert run.returncode != 0
    assert "modules/alpha" in run.stderr
    assert git("rev-parse", "HEAD", cwd=checkout / "modules" / "beta") == newest_beta


def test_a_module_whose_clone_was_cut_short_is_finished_on_the_next_run(hive):
    remotes, checkout = hive
    assert modules_sh(checkout).returncode == 0
    alpha = checkout / "modules" / "alpha"
    (alpha / "version").unlink()  # the .git is there, the files never were

    run = modules_sh(checkout)

    assert run.returncode == 0, run.stderr
    assert (alpha / "version").read_text() == "1"


def test_commits_made_on_a_detached_module_are_kept_and_named(hive):
    remotes, checkout = hive
    assert modules_sh(checkout).returncode == 0
    alpha = checkout / "modules" / "alpha"
    git("checkout", "-q", "--detach", cwd=alpha)
    (alpha / "version").write_text("detached work")
    git("commit", "-q", "-am", "detached work", cwd=alpha)
    work = git("rev-parse", "HEAD", cwd=alpha)
    remotes.push_new("alpha", "7")

    run = modules_sh(checkout, float_to_tips=True)

    assert run.returncode != 0
    assert "modules/alpha" in run.stderr
    assert git("rev-parse", "HEAD", cwd=alpha) == work


def _mirrored_hive(tmp_path, monkeypatch) -> Remotes:
    """A hive-mind whose manifest names GitHub-style URLs that do not resolve
    here, served from a local mirror the installer is pointed at."""
    remotes = Remotes(tmp_path)
    remotes.create("inference-proxy", "main")
    remotes.create("hive-data", "main")
    remotes.superproject({"inference-proxy": "main", "hive-data": "main"}, with_script=True)
    work = remotes.scratch / "hive-mind"
    for name in ("inference-proxy", "hive-data"):
        git("config", "-f", ".gitmodules", f"submodule.modules/{name}.url",
            f"https://upstream.invalid/{name}.git", cwd=work)
    git("commit", "-q", "-am", "manifest names upstream", cwd=work)
    git("push", "-q", remotes.url("hive-mind"), "HEAD:master", cwd=work)
    monkeypatch.setattr(steps, "REPO_BASE", str(remotes.base))
    # A real install has no help allowing local transport.
    for var in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(var)
    return remotes


def test_the_installer_clones_hive_mind_with_every_module_at_the_commit_the_release_records(tmp_path, monkeypatch):
    remotes = _mirrored_hive(tmp_path, monkeypatch)
    remotes.push_new("inference-proxy", "2")
    inst = installer_at(tmp_path / "install", answers())

    steps.clone_repos(steps.Ctx(inst, Shell(inst.home.log)))

    tools = inst.home.root / "repos" / "hive-mind" / "modules" / "inference-proxy"
    recorded = git("rev-parse", "HEAD:modules/inference-proxy", cwd=inst.home.repo("hive-mind"))
    assert inst.home.repo("inference-proxy") == tools
    assert git("rev-parse", "HEAD", cwd=tools) == recorded
    assert git("branch", "--show-current", cwd=tools) == ""
    assert (inst.home.repo("hive-data") / "version").read_text() == "1"


def test_the_install_stops_when_a_module_cannot_be_brought_up(tmp_path, monkeypatch):
    remotes = _mirrored_hive(tmp_path, monkeypatch)
    inst = installer_at(tmp_path / "install", answers())
    ctx = steps.Ctx(inst, Shell(inst.home.log))
    steps.clone_repos(ctx)
    tools = inst.home.repo("inference-proxy")
    (tools / "version").write_text("local")
    git("commit", "-q", "-am", "local only", cwd=tools)
    remotes.push_new("inference-proxy", "upstream")

    with pytest.raises(steps.StepFailed, match="modules/inference-proxy"):
        steps.clone_repos(ctx)


def test_a_module_on_a_branch_is_left_alone_and_named_with_the_commit_the_release_records(hive):
    remotes, checkout = hive
    assert modules_sh(checkout).returncode == 0
    alpha = checkout / "modules" / "alpha"
    git("checkout", "-q", "-b", "mine", cwd=alpha)
    (alpha / "version").write_text("mine")
    git("commit", "-q", "-am", "mine", cwd=alpha)
    mine = git("rev-parse", "HEAD", cwd=alpha)

    run = modules_sh(checkout)

    assert run.returncode == 0, run.stderr
    assert "left on mine" in run.stdout
    assert git("rev-parse", "HEAD", cwd=alpha) == mine


def test_a_module_with_uncommitted_changes_is_not_moved_and_the_run_fails(hive):
    remotes, checkout = hive
    assert modules_sh(checkout).returncode == 0
    alpha = checkout / "modules" / "alpha"
    newer = remotes.push_new("alpha", "next release")
    git("update-index", "--cacheinfo", f"160000,{newer},modules/alpha", cwd=checkout)
    git("commit", "-q", "-m", "release records a newer alpha", cwd=checkout)
    (alpha / "version").write_text("edited")

    run = modules_sh(checkout)

    assert run.returncode != 0
    assert "modules/alpha" in run.stderr
    assert (alpha / "version").read_text() == "edited"
